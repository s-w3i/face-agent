#!/usr/bin/env python3
"""Authoritative shared state: /robot/status and /robot/set_status."""
import argparse
import fcntl
import os
from pathlib import Path
import time
from robot_states import normalize_status


def main():
    import rclpy
    from rclpy.qos import QoSProfile, ReliabilityPolicy, DurabilityPolicy
    from robot_status_interfaces.msg import RobotStatus
    from robot_status_interfaces.srv import SetRobotStatus

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--initial-status', default='IDLE', type=normalize_status)
    args, ros_args = parser.parse_known_args()
    # Local autostarters share one authority, including when display/chat launch together.
    cache = Path(os.environ.get('XDG_CACHE_HOME', Path.home() / '.cache')) / 'face-agent'
    cache.mkdir(parents=True, exist_ok=True)
    lock = (cache / f'robot-status-{os.environ.get("ROS_DOMAIN_ID", "0")}.lock').open('w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print('Robot status manager is already running.', flush=True)
        return
    rclpy.init(args=ros_args)
    node = rclpy.create_node('robot_status_manager')
    qos = QoSProfile(depth=1, reliability=ReliabilityPolicy.RELIABLE,
                     durability=DurabilityPolicy.TRANSIENT_LOCAL)
    publisher = node.create_publisher(RobotStatus, '/robot/status', qos)
    current = RobotStatus(status=args.initial_status, revision=time.time_ns(), source='startup')
    publisher.publish(current)

    def set_status(request, response):
        nonlocal current
        try:
            status = normalize_status(request.status)
            if request.expected_revision and request.expected_revision != current.revision:
                raise ValueError('A newer status command has taken control.')
            current = RobotStatus(status=status, revision=current.revision + 1, source=request.source)
            publisher.publish(current)
            response.success = True
            response.message = 'Robot status: ' + status
        except ValueError as error:
            response.success = False
            response.message = str(error)
        response.current = current
        return response

    node.create_service(SetRobotStatus, '/robot/set_status', set_status)
    node.get_logger().info('Ready: /robot/status and /robot/set_status; initial ' + current.status)
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, rclpy.executors.ExternalShutdownException):
        pass
    finally:
        node.destroy_node()
        rclpy.try_shutdown()
        lock.close()


if __name__ == '__main__':
    main()
