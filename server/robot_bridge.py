"""LeRobot bridge – wraps SO-101 follower arm with thread-safe control loop."""

import asyncio
import json
import logging
import queue
import threading
import time

import cv2

logger = logging.getLogger(__name__)

# Joint names for SO-101 (6-DOF)
JOINT_NAMES = [
    "shoulder_pan",
    "shoulder_lift",
    "elbow_flex",
    "wrist_flex",
    "wrist_roll",
    "gripper",
]

# Arm joints only (without gripper) - used for home position
ARM_JOINTS = [j for j in JOINT_NAMES if j != "gripper"]

# Home resting position — shoulder_lift slightly forward is natural with gravity
HOME_POSITION = {
    "shoulder_pan.pos": 0.0,
    "shoulder_lift.pos": 0.0,
    "elbow_flex.pos": 0.0,
    "wrist_flex.pos": 0.0,
    "wrist_roll.pos": 0.0,
}


class RobotController:
    """Manages SO-101 follower arm connection, state reading, and action execution."""

    def __init__(self, config):
        self.config = config
        self.robot = None
        self.is_connected = False
        self._lock = threading.Lock()
        self._frames: dict[str, bytes] = {}  # camera_name -> jpeg bytes
        self._state: dict[str, float] = {}   # joint_name.pos -> value
        self._velocity: dict[str, float] = {}  # joint_name.pos -> speed per tick
        self._target: dict[str, float] | None = None  # smooth move target
        self._target_speed: float = 30.0  # degrees per second for smooth move
        self._hold_position: dict[str, float] = {}  # continuously reinforce this position
        self._action_queue: queue.Queue = queue.Queue(maxsize=10)
        self._control_thread: threading.Thread | None = None
        self._running = False
        self._state_subscribers: list[asyncio.Queue] = []

    def connect(self) -> str | None:
        """Connect to robot hardware. Returns error string or None on success."""
        if self.is_connected:
            return "Already connected"
        try:
            from lerobot.cameras.opencv import OpenCVCameraConfig
            from lerobot.robots.so_follower.so101_follower.so101_follower import (
                SO101Follower,
                SO101FollowerConfig,
            )

            camera_configs = {}
            # Try cameras individually — skip any that fail
            for name, idx in [("front", self.config.robot_camera_front),
                              ("top", self.config.robot_camera_top)]:
                camera_configs[name] = OpenCVCameraConfig(
                    index_or_path=idx, width=640, height=480, fps=30,
                )

            robot_config = SO101FollowerConfig(
                port=self.config.robot_port,
                id=self.config.robot_id,
                cameras=camera_configs,
            )

            def _is_motor6_error(err_str):
                return "6" in err_str and (
                    "Missing motor" in err_str or "id_=6" in err_str
                )

            def _is_camera_error(err_str):
                return "read failed" in err_str or "Camera" in err_str

            def _try_connect(config, skip_gripper=False):
                robot = SO101Follower(config)
                if skip_gripper and "gripper" in robot.bus.motors:
                    del robot.bus.motors["gripper"]
                robot.connect()
                return robot

            # Try all combinations: full → no gripper → no camera → no both
            last_err = None
            for skip_gripper in (False, True):
                for cams in (camera_configs, {}):
                    cfg = SO101FollowerConfig(
                        port=self.config.robot_port,
                        id=self.config.robot_id,
                        cameras=cams,
                    )
                    try:
                        self.robot = _try_connect(cfg, skip_gripper=skip_gripper)
                        if skip_gripper:
                            logger.warning("Connected without gripper motor (ID 6)")
                        if not cams:
                            logger.warning("Connected without cameras")
                        break
                    except Exception as err:
                        last_err = err
                        err_str = str(err)
                        if _is_motor6_error(err_str) and not skip_gripper:
                            break  # skip to next skip_gripper=True loop
                        if _is_camera_error(err_str) and cams:
                            continue  # try without cameras
                        raise
                else:
                    continue  # inner loop didn't break → try next skip_gripper
                break  # success
            else:
                raise last_err  # all combinations failed
            self.is_connected = True
            self._running = True

            # Read initial state and hold current position
            self._read_observation()
            self._hold_position = dict(self._state)

            self._control_thread = threading.Thread(
                target=self._control_loop, daemon=True, name="robot-control"
            )
            self._control_thread.start()
            logger.info("Robot connected: %s on %s", self.config.robot_id, self.config.robot_port)
            return None
        except Exception as e:
            logger.exception("Robot connection failed")
            self.is_connected = False
            return str(e)

    def disconnect(self) -> str | None:
        """Disconnect from robot. Returns error string or None on success."""
        if not self.is_connected:
            return "Not connected"
        try:
            self.clear_velocity()
            with self._lock:
                self._target = None
            self._running = False
            if self._control_thread:
                self._control_thread.join(timeout=3)
                self._control_thread = None
            if self.robot:
                self.robot.disconnect()
                self.robot = None
            self.is_connected = False
            with self._lock:
                self._frames.clear()
                self._state.clear()
            # Drain action queue
            while not self._action_queue.empty():
                try:
                    self._action_queue.get_nowait()
                except queue.Empty:
                    break
            logger.info("Robot disconnected")
            return None
        except Exception as e:
            logger.exception("Robot disconnect failed")
            return str(e)

    def set_velocity(self, velocity: dict[str, float]):
        """Set continuous velocity vector. Control loop applies this each tick."""
        with self._lock:
            self._velocity = {k: float(v) for k, v in velocity.items()}

    def clear_velocity(self):
        """Stop all velocity-based movement."""
        with self._lock:
            self._velocity.clear()

    def move_to(self, target: dict[str, float], speed: float = 30.0):
        """Start smooth movement towards target position at given speed (deg/s)."""
        with self._lock:
            self._target = {k: float(v) for k, v in target.items()}
            self._target_speed = speed
            self._velocity.clear()  # cancel manual velocity

    def stop(self):
        """Emergency stop – clear velocity, drain queue, hold position."""
        self.clear_velocity()
        with self._lock:
            self._target = None
        # Drain pending actions
        while not self._action_queue.empty():
            try:
                self._action_queue.get_nowait()
            except queue.Empty:
                break
        # Send current state as target (hold position)
        state = self.get_state()
        if state and self.is_connected:
            try:
                self._action_queue.put_nowait(state)
            except queue.Full:
                pass

    def send_action(self, action: dict, mode: str = "absolute"):
        """Queue a joint action. mode='absolute' or 'delta'."""
        if not self.is_connected:
            return
        if mode == "delta":
            current = self.get_state()
            resolved = {}
            for key, delta in action.items():
                cur = current.get(key, 0.0)
                resolved[key] = cur + float(delta)
            action = resolved
        # Drop old actions if queue full (keep latest)
        try:
            self._action_queue.put_nowait(action)
        except queue.Full:
            try:
                self._action_queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._action_queue.put_nowait(action)
            except queue.Full:
                pass

    def get_state(self) -> dict[str, float]:
        """Return current joint positions (thread-safe copy)."""
        with self._lock:
            return dict(self._state)

    def get_camera_frame(self, name: str) -> bytes | None:
        """Return latest JPEG-encoded camera frame."""
        with self._lock:
            return self._frames.get(name)

    def get_status(self) -> dict:
        """Return full status info."""
        return {
            "connected": self.is_connected,
            "joints": self.get_state(),
            "cameras": list(self._frames.keys()),
        }

    def subscribe_state(self) -> asyncio.Queue:
        """Create a state update queue for WebSocket broadcasting."""
        q: asyncio.Queue = asyncio.Queue(maxsize=5)
        self._state_subscribers.append(q)
        return q

    def unsubscribe_state(self, q: asyncio.Queue):
        """Remove a state subscriber."""
        if q in self._state_subscribers:
            self._state_subscribers.remove(q)

    # --- Internal ---

    def _read_observation(self):
        """Read robot observation and update internal state + camera frames."""
        if not self.robot:
            return
        try:
            obs = self.robot.get_observation()
        except ConnectionError:
            time.sleep(0.05)  # let serial bus recover
            return
        except Exception:
            logger.exception("Failed to read observation")
            return

        # Encode cameras outside lock to minimize lock hold time
        encoded = {}
        for cam in ("front", "top"):
            if cam in obs:
                try:
                    bgr = cv2.cvtColor(obs[cam], cv2.COLOR_RGB2BGR)
                    _, buf = cv2.imencode(
                        ".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, 60]
                    )
                    encoded[cam] = buf.tobytes()
                except Exception:
                    pass

        with self._lock:
            for key, val in obs.items():
                if key.endswith(".pos"):
                    self._state[key] = round(float(val), 2)
            self._frames.update(encoded)

        # Notify async subscribers (non-blocking)
        state_json = json.dumps(self._state)
        for q in list(self._state_subscribers):
            try:
                q.put_nowait(state_json)
            except (asyncio.QueueFull, Exception):
                pass

    def _control_loop(self):
        """Background thread: read state + execute actions at ~15 fps."""
        logger.info("Robot control loop started")
        target_dt = 1.0 / 15.0
        _tgt_prev_state = None  # for stall detection
        _tgt_stall_count = 0

        while self._running:
            t0 = time.monotonic()

            # Read current state & camera
            self._read_observation()

            # Smooth move to target (e.g. Home)
            with self._lock:
                tgt = dict(self._target) if self._target else {}
                tgt_speed = self._target_speed
            if tgt and self.robot and self.is_connected:
                current = self.get_state()
                action = {}
                all_arrived = True
                # Use larger step so servo PID generates enough torque against gravity
                move_lead = 0.5  # seconds of look-ahead
                for joint, goal in tgt.items():
                    cur = current.get(joint, 0.0)
                    diff = goal - cur
                    if abs(diff) < 1.0:
                        action[joint] = goal
                    else:
                        all_arrived = False
                        step = min(abs(diff), tgt_speed * move_lead)
                        action[joint] = cur + step * (1 if diff > 0 else -1)
                if action:
                    try:
                        self.robot.send_action(action)
                    except Exception:
                        logger.exception("Failed to send target action")
                # Stall detection: if position barely changes for 15 ticks (~1s), give up
                if not all_arrived:
                    cur_key = tuple(round(current.get(j, 0), 0) for j in tgt)
                    if cur_key == _tgt_prev_state:
                        _tgt_stall_count += 1
                    else:
                        _tgt_stall_count = 0
                        _tgt_prev_state = cur_key
                    if _tgt_stall_count >= 15:
                        logger.info("Target move stalled, giving up")
                        all_arrived = True
                if all_arrived:
                    _tgt_prev_state = None
                    _tgt_stall_count = 0
                    self._hold_position = dict(action) if action else {}
                    with self._lock:
                        self._target = None

            # Apply velocity: continuously move joints by velocity * dt
            # Use look-ahead > tick interval so servo PID generates enough torque
            # to overcome gravity on shoulder_lift / elbow_flex
            velocity_lead = 0.5  # seconds of look-ahead
            acted = False
            with self._lock:
                vel = dict(self._velocity) if self._velocity else {}
            if vel and not tgt and self.robot and self.is_connected:
                current = self.get_state()
                action = {}
                for joint, speed in vel.items():
                    cur = current.get(joint, 0.0)
                    if abs(speed) > 0.01:
                        action[joint] = cur + speed * velocity_lead
                    else:
                        action[joint] = cur  # brake
                if action:
                    try:
                        self.robot.send_action(action)
                        self._hold_position = dict(action)
                        acted = True
                    except Exception:
                        logger.exception("Failed to send velocity action")

            # Process queued actions (absolute/delta commands override velocity)
            try:
                action = self._action_queue.get_nowait()
                if self.robot and self.is_connected:
                    self.robot.send_action(action)
                    self._hold_position = dict(action)
                    acted = True
            except queue.Empty:
                pass
            except Exception:
                logger.exception("Failed to send action")

            # Active hold: gentle overshoot to resist gravity drift
            # drift×2, capped ±15° — enough torque without oscillation
            if not acted and not tgt and self._hold_position and self.robot and self.is_connected:
                try:
                    current = self.get_state()
                    hold_cmd = {}
                    for joint, goal in self._hold_position.items():
                        cur = current.get(joint, goal)
                        drift = goal - cur
                        overshoot = max(-15.0, min(15.0, drift * 2.0))
                        hold_cmd[joint] = goal + overshoot
                    self.robot.send_action(hold_cmd)
                except Exception:
                    pass

            # Maintain target framerate
            elapsed = time.monotonic() - t0
            sleep_time = target_dt - elapsed
            if sleep_time > 0:
                time.sleep(sleep_time)

        logger.info("Robot control loop stopped")
