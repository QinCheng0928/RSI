"""ProjectAirSim flight lifecycle, fixed PD+CBF navigator and isolated decisions.

No dependency on ACIAT-UAV at runtime. Only its robot/scene configuration and
navigation/lifecycle conventions are reused. All evaluation sees measured
flight observations; no training reward, score or reference objective is used.
"""
from __future__ import annotations

import asyncio
from collections import deque
from contextlib import contextmanager
import fcntl
import hashlib
import importlib.metadata
import json
import logging
import math
import os
from pathlib import Path
import select
import selectors
import shutil
import socket
import subprocess
import sys
import tempfile
import time

import numpy as np
from scipy.optimize import minimize

from evolution.config import ExecutionConfig
from evolution.errors import AlgorithmExecutionError, InfrastructureError
from evolution.models import Scenario, SimulatorResult, Status
from .spec import FlightScenario, POS_MAX, POS_MIN, validate_positions


DIRECTORY = Path(__file__).resolve().parent
SIMULATOR_VERSION = "projectairsim-0.1.1-fast-physics-uav-v1"
DT, MAX_SPEED, MAX_ACCEL, GOAL_TOLERANCE = 0.1, 10.0, 2.0, 3.0
TRACE_STRIDE = 25
WORKER_ENV = {"PATH": "/usr/bin:/bin", "PYTHONHASHSEED": "0", "LANG": "C.UTF-8"}


def harness_hash():
    paths = [DIRECTORY / "simulator.py", DIRECTORY / "spec.py",
             DIRECTORY / "template/algorithm.py.txt", *sorted((DIRECTORY / "sim_config").glob("*")),
             Path(__file__).parents[2] / "evolution/testing/loader.py"]
    return hashlib.sha256(b"".join(p.read_bytes() for p in paths)).hexdigest()


def clip(vector, limit):
    value = np.asarray(vector, dtype=float)
    return value * min(1.0, limit / max(float(np.linalg.norm(value)), 1e-12))


def validate_action(action, count):
    try:
        if not isinstance(action, dict) or set(action) != {"accelerations"}:
            raise ValueError("return exactly an accelerations object")
        rows = action["accelerations"]
        if not isinstance(rows, list) or len(rows) != count:
            raise ValueError("one acceleration vector is required for each adversary")
        if any(not isinstance(row, list) or len(row) != 3 or any(
                type(x) not in (int, float) or not math.isfinite(x) for x in row) for row in rows):
            raise ValueError("accelerations must be finite numeric vectors of length three")
        values = np.asarray(rows, dtype=float)
        if np.any(np.linalg.norm(values, axis=1) > MAX_ACCEL + 1e-8):
            raise ValueError("acceleration norm exceeds 2 m/s²")
        return values
    except (ValueError, TypeError, OverflowError) as error:
        raise AlgorithmExecutionError(str(error), "ACTION_CONTRACT") from error


def control(state, nominal, obstacles=()):
    """Original relative-degree-two boundary/obstacle CBF and PD-QP limits."""
    p, v = state[:3], state[3:6]
    rows, rhs = [], []
    for axis in range(3):
        unit = np.eye(3)[axis]
        rows.extend([-unit, unit, unit, -unit])
        rhs.extend([10 * v[axis] + 20 * (p[axis] - POS_MIN[axis]),
                    -10 * v[axis] + 20 * (POS_MAX[axis] - p[axis]),
                    (MAX_SPEED - v[axis]) / DT, (MAX_SPEED + v[axis]) / DT])
    for obstacle in obstacles:
        relative_p, relative_v = p - obstacle[:3], v - obstacle[3:6]
        h = relative_p @ relative_p - 2.0 ** 2
        rows.append(-2 * relative_p)
        rhs.append(2 * relative_v @ relative_v - 2 * relative_p @ obstacle[6:9]
                   + 0.8 * 2 * relative_p @ relative_v + 2.0 * h)
    a, b, nominal = np.asarray(rows), np.asarray(rhs), clip(nominal, MAX_ACCEL)
    result = minimize(lambda u: 0.5 * float((u - nominal) @ (u - nominal)), nominal,
                      jac=lambda u: u - nominal, bounds=[(-MAX_ACCEL, MAX_ACCEL)] * 3,
                      constraints=[{"type": "ineq", "fun": lambda u: b - a @ u,
                                    "jac": lambda u: -a}], method="SLSQP",
                      options={"ftol": 1e-6, "maxiter": 50})
    if result.success and np.all(np.isfinite(result.x)):
        return clip(result.x, MAX_ACCEL)
    # Preserve the original controller's finite projection fallback. Actual
    # bounds/collisions are checked independently; this is not a safety proof.
    value = nominal.copy()
    for _ in range(8):
        for row, limit in zip(a, b):
            violation = float(row @ value - limit)
            if violation > 0 and row @ row > 1e-12:
                value = np.clip(value - violation / (row @ row) * row, -MAX_ACCEL, MAX_ACCEL)
    return clip(value, MAX_ACCEL)


WORKER_CODE = r'''
import contextlib, io, json, sys, traceback
sys.path.insert(0, "/harness")
from pathlib import Path
from evolution.testing.loader import load_algorithm

class Log(io.TextIOBase):
    def __init__(self): self.value = ""
    def write(self, text):
        self.value = (self.value + text)[-4096:]
        return len(text)

log = Log()
def send(value):
    data = json.dumps(value, allow_nan=False, separators=(",", ":"))
    if len(data) > 65536: raise ValueError("decision output exceeds 64 KiB")
    sys.__stdout__.write(data + "\n")
    sys.__stdout__.flush()

try:
    with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
        solve = load_algorithm(Path("/candidate/algorithm.py"))
    send({"ready": True})
    for line in sys.stdin:
        request = json.loads(line)
        with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
            action = solve(request["observation"], request["seed"])
        send({"action": action, "log": log.value})
        log.value = ""
except BaseException:
    send({"error": traceback.format_exc(limit=5)[-4096:], "log": log.value})
'''


def sandbox_command(config, bindings):
    command = ["bwrap", "--unshare-all", "--die-with-parent", "--new-session",
               "--cap-drop", "ALL", "--setenv", "PATH", "/usr/bin",
               "--setenv", "PYTHONHASHSEED", "0"]
    for path in ("/usr", "/lib", "/lib64", "/bin"):
        if Path(path).exists():
            command.extend(["--ro-bind", path, path])
    command.extend(["--proc", "/proc", "--dev", "/dev", "--dir", "/tmp"])
    for source, target in bindings:
        command.extend(["--ro-bind", str(source), target])
    return command + ["--remount-ro", "/", "--chdir", "/tmp", "--", "/usr/bin/prlimit",
                      f"--as={config.memory_mb * 1024 * 1024}",
                      f"--cpu={math.ceil(config.timeout_seconds)}", "--fsize=0", "--nofile=64", "--nproc=16",
                      "--", "/usr/bin/python3", "-I", "-S", "-B"]


class DecisionWorker:
    """Persistent flight-local process, with OS isolation and bounded JSON IPC."""
    def __init__(self, directory, config):
        self.config, self.log, self.buffer = config, "", b""
        self.temporary = tempfile.TemporaryDirectory(prefix="uav-decision-")
        worker = Path(self.temporary.name) / "worker.py"
        worker.write_text(WORKER_CODE)
        root = Path(__file__).parents[2]
        bindings = [(directory / "algorithm.py", "/candidate/algorithm.py"), (worker, "/worker.py"),
                    (root / "evolution/testing/loader.py", "/harness/evolution/testing/loader.py"),
                    (root / "evolution/errors.py", "/harness/evolution/errors.py")]
        self.process = subprocess.Popen(sandbox_command(config, bindings) + ["/worker.py"],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, bufsize=0, env=WORKER_ENV)
        os.set_blocking(self.process.stdin.fileno(), False)
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.process.stdout, selectors.EVENT_READ)
        try:
            if self.receive(5).get("ready") is not True:
                raise AlgorithmExecutionError("algorithm import failed", "IMPORT_ERROR")
        except BaseException:
            self.close()
            raise

    def receive(self, timeout):
        deadline = time.monotonic() + timeout
        while b"\n" not in self.buffer:
            if not self.selector.select(max(0, deadline - time.monotonic())):
                raise AlgorithmExecutionError("decision time limit exceeded", "DECISION_TIMEOUT")
            data = os.read(self.process.stdout.fileno(), 8192)
            if not data:
                raise AlgorithmExecutionError("decision worker exited: " + self.buffer.decode(errors="replace"), "WORKER_EXIT")
            self.buffer += data
            if len(self.buffer) > 131072:
                raise AlgorithmExecutionError("worker output limit exceeded", "OUTPUT_LIMIT")
        line, self.buffer = self.buffer.split(b"\n", 1)
        try:
            result = json.loads(line)
            if not isinstance(result, dict):
                raise ValueError("worker response must be an object")
        except (ValueError, UnicodeError) as error:
            if line.startswith((b"bwrap:", b"prlimit:")):
                raise InfrastructureError("decision sandbox failed: " + line.decode(errors="replace")) from error
            raise AlgorithmExecutionError("invalid worker protocol", "WORKER_PROTOCOL") from error
        self.log = (self.log + str(result.get("log", "")))[-8192:]
        if "error" in result:
            raise AlgorithmExecutionError(result["error"], "PYTHON_ERROR")
        return result

    def solve(self, observation, seed):
        request = json.dumps({"observation": observation, "seed": seed}, allow_nan=False).encode() + b"\n"
        timeout = float(self.config.environment["decision_timeout_seconds"])
        deadline = time.monotonic() + timeout
        try:
            pending = memoryview(request)
            while pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not select.select([], [self.process.stdin], [], remaining)[1]:
                    raise AlgorithmExecutionError("decision input time limit exceeded", "DECISION_TIMEOUT")
                pending = pending[os.write(self.process.stdin.fileno(), pending):]
        except (BrokenPipeError, OSError) as error:
            raise AlgorithmExecutionError("decision worker closed its input", "WORKER_EXIT") from error
        result = self.receive(max(0, deadline - time.monotonic()))
        if "action" not in result:
            raise AlgorithmExecutionError("missing action in worker response", "WORKER_PROTOCOL")
        return result["action"]

    def close(self):
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait(timeout=5)
        self.selector.close()
        self.process.stdin.close()
        self.process.stdout.close()
        self.temporary.cleanup()


class Flight:
    def __init__(self, config):
        from projectairsim import ProjectAirSimClient
        logger = logging.getLogger("projectairsim")
        if not logger.hasHandlers():
            logger.addHandler(logging.StreamHandler())
            logger.setLevel(logging.WARNING)
        env = config.environment
        self.config = config
        self.client = ProjectAirSimClient(address=env["address"], port_topics=env["port_topics"],
                                          port_services=env["port_services"])
        self.world, self.drones, self.collision = None, [], []
        self.rpc_timeout_seconds = float(env.get("rpc_timeout_seconds", 300))
        self.takeoff_timeout_seconds = float(env.get("takeoff_timeout_seconds", 20))
        self.landing_timeout_seconds = float(env.get("landing_timeout_seconds", 120))
        self._closed = False

    def _run_async(self, awaitable):
        """Run SDK coroutines from the synchronous harness, as in ACIAT-UAV."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            if hasattr(awaitable, "close"):
                awaitable.close()
            raise RuntimeError(
                "ProjectAirSim Flight was called from an active asyncio loop. "
                "Use an async flight method or run the simulator from synchronous code."
            )
        # Outside the except block: an SDK failure must not be chained to the
        # expected 'no running event loop' detection above.
        asyncio.run(awaitable)

    async def _lifecycle(self, method, timeout_seconds, require_success=True):
        replies = [None] * len(self.drones)

        async def submit(index, drone):
            def on_reply(result):
                replies[index] = result
            return await getattr(drone, method)(timeout_sec=timeout_seconds, callback=on_reply)

        tasks = await asyncio.gather(*[submit(i, drone) for i, drone in enumerate(self.drones)])
        results = await asyncio.gather(*tasks)
        # SDK 0.1.1 sends the actual RPC result to the callback but returns None
        # from its task. Inspect both, so a timed-out server command returning
        # false is not mistaken for successful takeoff/landing.
        failed = [drone.name for drone, reply, result in zip(self.drones, replies, results)
                  if reply is False or result is False or (reply is None and result is None)]
        if failed and require_success:
            raise InfrastructureError(f"{method} did not succeed for {failed} (command timeout {timeout_seconds}s of simulation time)")
        return failed

    async def _takeoff(self):
        from projectairsim.types import LandedState
        incomplete = await self._lifecycle("takeoff_async", self.takeoff_timeout_seconds, require_success=False)
        # The SDK goal is 3 m above the spawn, not the task's initial position.
        # An airborne vehicle can hand over to the requested-state reset even
        # when that ascent goal timed out, as in the original environment.
        not_flying = [d.name for d in self.drones if d.get_landed_state() != LandedState.FLYING]
        if not_flying:
            raise InfrastructureError(f"takeoff did not make UAVs airborne: {not_flying}")
        if incomplete:
            logging.getLogger(__name__).warning(
                "SDK takeoff altitude target was not reached by %s; FLYING state verified before scenario reset",
                incomplete)

    async def _land(self):
        await self._lifecycle("land_async", self.landing_timeout_seconds)

    @staticmethod
    def _spawn_positions(initial_positions):
        # SDK takeoff adds -3 m in NED. Spawn at z=-1 so it stays inside the
        # flight box, including scenarios whose desired initial z is near -7.
        # Separate vertically stacked starts horizontally during this phase.
        grid = [(x, y) for x in np.arange(POS_MIN[0] + 1, POS_MAX[0], 2)
                for y in np.arange(POS_MIN[1] + 1, POS_MAX[1], 2)]
        positions = []
        for target in initial_positions:
            choices = [tuple(target[:2]), *grid]
            valid = [p for p in choices if all(math.dist(p, q[:2]) > 1 for q in positions)]
            if not valid:
                raise InfrastructureError("no separated takeoff positions available")
            x, y = min(valid, key=lambda p: math.dist(p, target[:2]))
            positions.append([float(x), float(y), POS_MAX[2]])
        validate_positions(positions)
        return positions

    def reset(self, scenario):
        from projectairsim import Drone, World
        self.client.connect()
        self.client.socket_services.recv_timeout = int(self.rpc_timeout_seconds * 1000)
        self.client.unsubscribe_all()
        self.temporary = tempfile.TemporaryDirectory(prefix="uav-scene-")
        directory = Path(self.temporary.name)
        shutil.copyfile(DIRECTORY / "sim_config/robot_quadrotor_classic.jsonc",
                        directory / "robot_quadrotor_classic.jsonc")
        scene = json.loads((DIRECTORY / "sim_config/scene_drone_classic.jsonc").read_text())
        prototype = scene["actors"][0]
        # The local SDK jitters actors named Drone*. Scenario-controlled names
        # bypass that hook without modifying the shared ProjectAirSim SDK.
        spawn_positions = self._spawn_positions(scenario.initial_positions)
        scene["actors"] = [{**prototype, "name": f"UAV{i + 1}",
                            "origin": {"xyz": " ".join(map(str, position)), "rpy": "0 0 0"}}
                           for i, position in enumerate(spawn_positions)]
        (directory / "scene.jsonc").write_text(json.dumps(scene))
        self.world = World(self.client, "scene.jsonc", delay_after_load_sec=2, sim_config_path=str(directory))
        self.drones = [Drone(self.client, self.world, actor["name"]) for actor in scene["actors"]]
        for drone in self.drones:
            if drone.enable_api_control() is False or drone.arm() is False:
                raise InfrastructureError("could not enable/arm drone")
        self._run_async(self._takeoff())
        self.world.pause()
        # Reset measured kinematics after takeoff, so every paired flight starts
        # at the requested airborne position with zero velocity/acceleration.
        for drone, position in zip(self.drones, scenario.initial_positions):
            state = drone.get_ground_truth_kinematics()
            state["pose"]["position"] = dict(zip("xyz", position))
            for key in ("twist", "accels"):
                for component in ("linear", "angular"):
                    state[key][component] = dict.fromkeys("xyz", 0.0)
            if drone.set_ground_truth_kinematics(state) is False:
                raise InfrastructureError("could not initialize airborne kinematics")
        self.collision = [False] * len(self.drones)
        self.start_ns = self.world.get_sim_time()
        for i, drone in enumerate(self.drones):
            def on_collision(topic, message, index=i):
                if message.get("time_stamp", 0) >= self.start_ns:
                    self.collision[index] = True
            self.client.subscribe(drone.robot_info["collision_info"], on_collision)
        states = self.observe()
        try:
            validate_positions([s[:3].tolist() for s in states])
        except ValueError as error:
            raise InfrastructureError("invalid measured initialization: " + str(error)) from error
        if any(np.linalg.norm(s[:3] - p) > 0.05 or np.linalg.norm(s[3:]) > 0.05
               for s, p in zip(states, scenario.initial_positions)):
            raise InfrastructureError("measured reset state differs from requested initial state")
        return states

    def observe(self):
        states = []
        for drone in self.drones:
            data = drone.get_ground_truth_kinematics()
            xyz = lambda value: [value[k] for k in "xyz"]
            state = np.array(xyz(data["pose"]["position"]) + xyz(data["twist"]["linear"])
                             + xyz(data["accels"]["linear"]), dtype=float)
            if not np.all(np.isfinite(state)):
                raise InfrastructureError("ProjectAirSim returned nonfinite kinematics")
            states.append(state)
        return np.asarray(states)

    def advance(self, velocities):
        commands = [(drone, float(v[0]), float(v[1]), float(v[2]))
                    for drone, v in zip(self.drones, velocities)]
        self._run_async(self._move_by_velocities(commands, DT))

    async def _move_by_velocities(self, commands, duration):
        tasks = await asyncio.gather(*[drone.move_by_velocity_async(
            v_north=v_north, v_east=v_east, v_down=v_down, duration=duration)
            for drone, v_north, v_east, v_down in commands])
        step_ns = int(self.world.config_dict["clock"]["step-ns"])
        delta_time_ns = int(round(duration * 1_000_000_000) + 100000)
        self.world.continue_for_sim_time(delta_time_ns + step_ns, wait_until_complete=True)
        results = await asyncio.gather(*tasks)
        if any(result is False for result in results):
            raise InfrastructureError("ProjectAirSim velocity command failed")

    def close(self):
        if self._closed:
            return
        self._closed = True
        errors = []
        if self.world is not None and self.client.state:
            try:
                if self.world.is_paused():
                    self.world.resume()
                self._run_async(self._land())
            except Exception as error:
                errors.append(f"landing: {type(error).__name__}: {error}")
            for drone in self.drones:
                if not self.client.state:
                    break
                for operation in (drone.disable_api_control, drone.disarm):
                    if not self.client.state:
                        break
                    try:
                        operation()
                    except Exception as error:
                        errors.append(f"{drone.name} control cleanup: {type(error).__name__}: {error}")
        elif self.world is not None:
            errors.append("connection already closed; skipped landing and control RPCs")
        try:
            # Also handles a partially failed connection, before a receive
            # thread or both sockets exist (SDK disconnect assumes they do).
            if self.client.state:
                self.client.disconnect()
            else:
                for name in ("socket_topics", "socket_services"):
                    stream = getattr(self.client, name, None)
                    if stream is not None and not getattr(stream, "closed", False):
                        stream.close()
        except Exception as error:
            errors.append(str(error))
        finally:
            if hasattr(self, "temporary"):
                self.temporary.cleanup()
        if errors:
            raise InfrastructureError("flight cleanup failed: " + "; ".join(errors))


@contextmanager
def simulator_lock(config):
    # Runs with different run IDs still share the same external UE server.
    path = Path(tempfile.gettempdir()) / ("llm-uav-" + hashlib.sha256(json.dumps(
        [config.environment[k] for k in ("address", "port_topics", "port_services")]).encode()).hexdigest()[:16] + ".lock")
    with path.open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise InfrastructureError("another experiment is controlling this ProjectAirSim server") from error
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


class ProjectAirSimSimulator:
    sandboxed = True
    deterministic = False

    def preflight(self, config: ExecutionConfig):
        if config.deterministic or config.cache_enabled:
            raise InfrastructureError("live UE/ProjectAirSim flights require deterministic=false and cache_enabled=false")
        if config.simulator_version != SIMULATOR_VERSION or config.harness_hash != harness_hash():
            raise InfrastructureError("UAV simulator version/harness fingerprint mismatch")
        if config.observation_schema_version != "uav-ned-v1":
            raise InfrastructureError("unsupported UAV observation schema")
        for name, default in (("decision_timeout_seconds", None), ("rpc_timeout_seconds", 300),
                              ("takeoff_timeout_seconds", 20),
                              ("landing_timeout_seconds", 120)):
            value = config.environment.get(name, default)
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise InfrastructureError(name + " must be finite and positive")
        for binary in ("bwrap", "prlimit", "/usr/bin/python3"):
            if shutil.which(binary) is None:
                raise InfrastructureError("missing sandbox prerequisite: " + binary)
        probe = subprocess.run(sandbox_command(config, []) + ["-c",
            "import os,socket; assert not os.path.exists('/home/qincheng/LLM'); "
            "assert not os.path.exists('/home/qincheng/ACIAT-UAV'); "
            "assert not os.access('/tmp',os.W_OK); assert not os.environ.get('OPENAI_API_KEY')"],
            capture_output=True, timeout=10, env=WORKER_ENV)
        if probe.returncode:
            raise InfrastructureError("decision sandbox unavailable: " + probe.stderr.decode(errors="replace")[-2000:])
        try:
            import projectairsim
            if importlib.metadata.version("projectairsim") != "0.1.1":
                raise InfrastructureError("expected the original ProjectAirSim SDK version 0.1.1")
            self.runtime = {"sdk": importlib.metadata.version("projectairsim"),
                            "numpy": np.__version__, "scipy": importlib.metadata.version("scipy"),
                            "sdk_source_hash": hashlib.sha256(b"".join(p.read_bytes() for p in
                                sorted(Path(projectairsim.__file__).parent.rglob("*.py")))).hexdigest()}
            for port in (config.environment["port_topics"], config.environment["port_services"]):
                with socket.create_connection((config.environment["address"], port), timeout=3):
                    pass
        except (ImportError, OSError, importlib.metadata.PackageNotFoundError) as error:
            raise InfrastructureError("ProjectAirSim unavailable: " + str(error)) from error

    def run(self, code_directory: Path, scenario: Scenario, config: ExecutionConfig) -> SimulatorResult:
        parameters = FlightScenario.model_validate(scenario.inputs)
        decisions, trajectories, episodes, logs = [], [], [], []
        deadline = time.monotonic() + config.timeout_seconds
        try:
            with simulator_lock(config):
                for episode_id, intensity in enumerate(parameters.intensities):
                    worker, flight = None, None
                    phase = "decision worker startup"
                    try:
                        worker = DecisionWorker(Path(code_directory), config)
                        phase = "initialization"
                        flight = Flight(config)
                        states = flight.reset(parameters)
                        initial = states[:, :3].tolist()
                        previous = np.zeros((parameters.num_adversaries, 3))
                        history = deque(maxlen=5)
                        episode = {"episode": episode_id, "intensity": intensity,
                                   "initial_positions": initial, "complete": False,
                                   "checked_steps": 0, "violations": [], "termination": "running"}
                        episodes.append(episode)
                        trajectories.append(self.frame(episode_id, 0, states, flight))
                        for step in range(parameters.max_steps):
                            phase = f"decision step {step}"
                            if time.monotonic() >= deadline:
                                raise InfrastructureError("flight run wall-clock time limit exceeded")
                            snapshot = {"step": step, "tested": self.state(states[0]),
                                        "adversaries": [self.state(s) for s in states[1:]]}
                            observation = {**snapshot, "time": (flight.world.get_sim_time() - flight.start_ns) / 1e9,
                                "dt": DT, "intensity": intensity, "num_adversaries": parameters.num_adversaries,
                                "remaining_steps": parameters.max_steps - step, "goal": parameters.goal,
                                "airspace": {"min": POS_MIN, "max": POS_MAX},
                                "limits": {"max_speed": MAX_SPEED, "max_acceleration": MAX_ACCEL,
                                           "min_separation": 1.0, "goal_tolerance": GOAL_TOLERANCE},
                                "previous_accelerations": previous.tolist(), "history": list(history)}
                            requested = validate_action(worker.solve(observation, scenario.seed), parameters.num_adversaries)
                            nominal = clip(0.4 * (np.asarray(parameters.goal) - states[0, :3])
                                           - 0.6 * states[0, 3:6] - 0.05 * states[0, 6:9], MAX_ACCEL)
                            applied = np.asarray([control(states[0], nominal, states[1:]),
                                *[control(s, u) for s, u in zip(states[1:], requested)]])
                            velocities = np.asarray([clip(s[3:6] + u * DT, MAX_SPEED) for s, u in zip(states, applied)])
                            history.append(snapshot)
                            previous = applied[1:]
                            phase = f"flight control step {step}"
                            flight.advance(velocities)
                            states = flight.observe()
                            episode["checked_steps"] = step + 1
                            positions = states[:, :3]
                            if any(flight.collision):
                                episode["violations"].append({"step": step + 1, "kind": "collision", "drones": list(flight.collision)})
                            for i in range(len(states)):
                                for j in range(i):
                                    if np.linalg.norm(positions[i] - positions[j]) <= 1:
                                        episode["violations"].append({"step": step + 1, "kind": "separation", "pair": [j, i]})
                            outside = [i for i, p in enumerate(positions) if np.any(p < POS_MIN) or np.any(p > POS_MAX)]
                            if any(i > 0 for i in outside):
                                episode["violations"].append({"step": step + 1, "kind": "adversary_outside", "drones": outside})
                            if episode["violations"]:
                                reason = "physical_constraint_violation"
                            elif 0 in outside:
                                reason = "tested_left_airspace"
                            elif np.linalg.norm(positions[0] - parameters.goal) <= GOAL_TOLERANCE:
                                reason = "tested_reached_goal"
                            elif step + 1 == parameters.max_steps:
                                reason = "time_limit"
                            else:
                                reason = "running"
                            if step % TRACE_STRIDE == 0 or reason != "running":
                                decisions.append({"episode": episode_id, "step": step,
                                    "requested_accelerations": requested.round(5).tolist(),
                                    "applied_accelerations": applied.round(5).tolist()})
                                trajectories.append(self.frame(episode_id, step + 1, states, flight))
                            if reason != "running":
                                episode.update(complete=True, termination=reason)
                                break
                        logs.append(worker.log)
                    finally:
                        primary_error = sys.exc_info()[1]
                        cleanup_errors = []
                        # Stop candidate code before touching the live flight lifecycle.
                        for resource in (worker, flight):
                            if resource is not None:
                                try:
                                    resource.close()
                                except Exception as error:
                                    cleanup_errors.append(f"{type(error).__name__}: {error}")
                        if cleanup_errors:
                            details = "; ".join(cleanup_errors)
                            if primary_error is not None:
                                raise InfrastructureError(
                                    f"scenario {scenario.scenario_id}, intensity {intensity}, {phase} failed: "
                                    f"{type(primary_error).__name__}: {primary_error}; cleanup also failed: {details}"
                                ) from primary_error
                            raise InfrastructureError(
                                f"scenario {scenario.scenario_id}, intensity {intensity}, cleanup failed: {details}")
        except AlgorithmExecutionError as error:
            return SimulatorResult(status=Status.INVALID, decisions=decisions, trajectories=trajectories,
                                   context={"episodes": episodes}, failure_category=error.category, stderr=str(error))
        except InfrastructureError:
            raise
        except Exception as error:
            raise InfrastructureError("ProjectAirSim flight failed: " + str(error)) from error
        return SimulatorResult(status=Status.EXECUTABLE, decisions=decisions, trajectories=trajectories,
            context={"episodes": episodes, "trace_stride": TRACE_STRIDE,
                     "safety_monitor_stride": 1, "runtime": getattr(self, "runtime", {})},
            stdout="\n".join(logs)[-8192:])

    @staticmethod
    def state(value):
        return {"position": value[:3].tolist(), "velocity": value[3:6].tolist(),
                "acceleration": value[6:9].tolist()}

    @staticmethod
    def frame(episode, step, states, flight):
        return {"episode": episode, "step": step,
                "time": (flight.world.get_sim_time() - flight.start_ns) / 1e9,
                "positions": states[:, :3].tolist(), "velocities": states[:, 3:6].round(4).tolist(),
                "collisions": list(flight.collision)}
