#!/usr/bin/env python3
"""MCP tools backed by native ROS 2 topics and the turtlesim rotation action."""
import asyncio
import json
import math
import os
from pathlib import Path
import threading
import time

import rclpy
from rclpy.action import ActionClient
from rclpy.node import Node
from geometry_msgs.msg import Twist
from turtlesim.msg import Pose
from turtlesim.action import RotateAbsolute
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError


def steering(pose, target, reverse=False):
    dx, dy = target['x'] - pose.x, target['y'] - pose.y
    distance = math.hypot(dx, dy)
    angle = math.atan2(dy, dx) - pose.theta - (math.pi if reverse else 0)
    error = math.atan2(math.sin(angle), math.cos(angle))
    return distance, (-1 if reverse else 1) * min(1.5, distance) if abs(error) < .3 else 0.0, max(-2.0, min(2.0, 4 * error))


class TurtleBridge(Node):
    def __init__(self, waypoint_file=None):
        super().__init__('mimo_turtlesim_mcp')
        path = Path(waypoint_file or os.environ.get('TURTLESIM_WAYPOINTS', Path(__file__).resolve().parents[1] / 'turtlesim-waypoints.json'))
        self.waypoints = json.loads(path.read_text())
        if not isinstance(self.waypoints, dict) or not self.waypoints:
            raise ValueError('Waypoints must be a nonempty object.')
        for name, point in self.waypoints.items():
            if not isinstance(name, str) or not isinstance(point, dict) or set(point) != {'x', 'y'} or any(type(point[k]) not in (int, float) or not math.isfinite(point[k]) or not .5 <= point[k] <= 10.5 for k in ('x', 'y')):
                raise ValueError('Each waypoint needs a name and finite x/y coordinates between 0.5 and 10.5.')
        self.lock = threading.RLock()
        self.pose = None; self.pose_at = 0
        self.target = None; self.deadline = 0; self.goal_handle = None; self.rotation_pending = False
        self.revision = 0; self.state = 'idle'; self.destination = None
        self.reverse = False; self.motion_kind = None
        self.require_announcement = os.environ.get('TURTLESIM_REQUIRE_ANNOUNCEMENT', '1') == '1'
        self.queued_theta = None
        self.publisher = self.create_publisher(Twist, '/turtle1/cmd_vel', 10)
        self.create_subscription(Pose, '/turtle1/pose', self.on_pose, 10)
        self.rotation = ActionClient(self, RotateAbsolute, '/turtle1/rotate_absolute')
        self.create_timer(.05, self.tick)

    def on_pose(self, pose):
        with self.lock:
            self.pose = pose; self.pose_at = time.monotonic()

    def velocity(self, linear=0.0, angular=0.0):
        message = Twist(); message.linear.x = float(linear); message.angular.z = float(angular)
        self.publisher.publish(message)

    def status(self):
        with self.lock:
            return dict(state=self.state, destination=self.destination, motion_id=self.revision, kind=self.motion_kind,
                        connected=self.pose is not None and time.monotonic() - self.pose_at < 1,
                        pose=dict(x=self.pose.x, y=self.pose.y, theta=self.pose.theta) if self.pose else None)

    def stop(self):
        with self.lock:
            self.revision += 1; self.target = None; self.rotation_pending = False
            self.queued_theta = None
            if self.goal_handle:
                self.goal_handle.cancel_goal_async(); self.goal_handle = None
            self.velocity(); self.state = 'stopped'
            return self.status()

    def go(self, destination):
        with self.lock:
            name = destination.strip().lower().replace('_', ' ')
            if name not in self.waypoints:
                raise ToolError('Unknown destination. Use list_destinations.')
            return self.start_motion(self.waypoints[name], name, 'room')

    def start_motion(self, target, destination, kind, reverse=False):
        if not self.status()['connected']:
            raise ToolError('No fresh turtle pose. Start turtlesim in the same ROS domain.')
        if self.target or self.queued_theta is not None or self.rotation_pending or self.goal_handle:
            raise ToolError('The turtle is moving. Stop it before sending a new command.')
        self.revision += 1; self.target = target; self.destination = destination
        self.motion_kind = kind; self.reverse = reverse
        self.deadline = time.monotonic() + 60
        self.state = 'queued' if self.require_announcement else 'moving'
        return self.status()

    def straight(self, distance=1.0):
        if not math.isfinite(distance) or not .1 <= abs(distance) <= 10:
            raise ToolError('Distance must be between 0.1 and 10 units; negative means backwards.')
        with self.lock:
            if not self.status()['connected']:
                raise ToolError('No fresh turtle pose. Start turtlesim first.')
            target = dict(x=self.pose.x + distance * math.cos(self.pose.theta),
                          y=self.pose.y + distance * math.sin(self.pose.theta))
            if any(not .5 <= coordinate <= 10.5 for coordinate in target.values()):
                raise ToolError('That distance would reach the edge of turtlesim. Choose a shorter distance or turn first.')
            return self.start_motion(target, None, 'straight', reverse=distance < 0)

    async def turn(self, direction, degrees=90.0):
        if direction not in ('left', 'right') or not math.isfinite(degrees) or not 1 <= degrees <= 180:
            raise ToolError('Use left or right with an angle between 1 and 180 degrees.')
        with self.lock:
            if not self.status()['connected']:
                raise ToolError('No fresh turtle pose. Start turtlesim first.')
            angle = self.pose.theta + math.radians(degrees) * (1 if direction == 'left' else -1)
            return await self.rotate(math.atan2(math.sin(angle), math.cos(angle)))

    def tick(self):
        with self.lock:
            if not self.target or self.state == 'queued':
                return
            if not self.status()['connected'] or time.monotonic() > self.deadline:
                self.velocity(); self.target = None; self.state = 'failed'
                return
            distance, linear, angular = steering(self.pose, self.target, self.reverse)
            if distance < .12:
                self.velocity(); self.target = None; self.state = 'arrived'
            else:
                self.velocity(linear, angular)

    async def rotate(self, theta, start_now=False):
        if not math.isfinite(theta) or not -math.pi <= theta <= math.pi:
            raise ToolError('Heading must be radians between -pi and pi.')
        with self.lock:
            if self.target or self.queued_theta is not None or self.rotation_pending or self.goal_handle:
                raise ToolError('The turtle is moving. Stop it first.')
            if not self.status()['connected'] or not self.rotation.server_is_ready():
                raise ToolError('Turtlesim rotation action is unavailable.')
            if not start_now:
                self.revision += 1
            self.destination = None; self.motion_kind = 'turn'
            if self.require_announcement and not start_now:
                self.queued_theta = theta; self.state = 'queued'
                return self.status()
            self.rotation_pending = True; self.state = 'rotating'; revision = self.revision
            goal = RotateAbsolute.Goal(); goal.theta = float(theta)
            future = self.rotation.send_goal_async(goal)
        def accepted(completed):
            with self.lock:
                try:
                    handle = completed.result()
                    if revision != self.revision:
                        if handle.accepted: handle.cancel_goal_async()
                        return
                    self.rotation_pending = False
                    if not handle.accepted:
                        self.state = 'failed'; return
                    self.goal_handle = handle
                    def finished(result):
                        with self.lock:
                            if revision == self.revision:
                                self.goal_handle = None
                                self.state = 'arrived' if result.result().status == 4 else 'failed'
                    handle.get_result_async().add_done_callback(finished)
                except Exception:
                    self.rotation_pending = False; self.state = 'failed'
        future.add_done_callback(accepted)
        return self.status()

    async def start_queued(self, motion_id):
        with self.lock:
            if self.state != 'queued' or motion_id != self.revision:
                raise ToolError('The queued motion was cancelled or replaced.')
            if not self.status()['connected']:
                self.target = None; self.queued_theta = None; self.state = 'failed'
                raise ToolError('Pose feedback was lost before movement could start.')
            if self.queued_theta is not None:
                theta = self.queued_theta; self.queued_theta = None
                return await self.rotate(theta, start_now=True)
            self.state = 'moving'; self.deadline = time.monotonic() + 60
            return self.status()


def make_server(bridge):
    mcp = MCPServer('Turtlesim robot')

    @mcp.tool()
    def list_destinations() -> dict:
        """List fixed room destinations in turtlesim coordinates."""
        return bridge.waypoints

    @mcp.tool()
    def robot_status() -> dict:
        """Read live pose, connection, and motion state. Only arrived confirms arrival."""
        return bridge.status()

    @mcp.tool()
    def go_to_room(destination: str) -> dict:
        """Queue movement to a named room. The runtime releases it after your spoken announcement; use robot_status to check arrival."""
        return bridge.go(destination)

    @mcp.tool()
    def stop_robot() -> dict:
        """Stop topic-driven motion and cancel an active rotation action."""
        return bridge.stop()

    @mcp.tool()
    async def rotate_robot(theta: float) -> dict:
        """Queue turtlesim's native RotateAbsolute action. Heading is radians (-pi to pi). The runtime starts it after your spoken announcement."""
        return await bridge.rotate(theta)

    @mcp.tool()
    def move_straight(distance: float = 1.0) -> dict:
        """Queue movement along the current heading. Default one unit; one requested meter equals one simulation unit. Negative distance moves backwards. Runtime starts after your spoken announcement."""
        return bridge.straight(distance)

    @mcp.tool()
    async def turn_robot(direction: str, degrees: float = 90.0) -> dict:
        """Queue a left or right turn relative to the current heading using the ROS action. Default 90 degrees; supports 1..180. Runtime starts after your spoken announcement."""
        return await bridge.turn(direction, degrees)

    @mcp.tool()
    async def start_queued_motion(motion_id: int) -> dict:
        """Runtime-only: release a queued motion after the spoken announcement completes."""
        return await bridge.start_queued(motion_id)

    return mcp


def main():
    rclpy.init()
    bridge = TurtleBridge()
    worker = threading.Thread(target=rclpy.spin, args=(bridge,), daemon=True)
    worker.start()
    try:
        make_server(bridge).run(transport='stdio')
    finally:
        bridge.stop()
        rclpy.shutdown(); worker.join(timeout=2); bridge.destroy_node()


if __name__ == '__main__':
    main()
