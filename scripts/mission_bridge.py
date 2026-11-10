#!/usr/bin/python3
"""ROS-only JSON-lines transport; never interprets commands or commands motors."""
import json
import sys
import threading
import time
import rclpy
from rclpy.node import Node
from rclpy.qos import QoSProfile, DurabilityPolicy, ReliabilityPolicy
from std_msgs.msg import String
from std_srvs.srv import Trigger
from robot_ai.srv import SubmitMission, AcknowledgePickSpeech


def main():
    rclpy.init()
    node = Node('face_mission_bridge')
    lock = threading.Lock()
    def emit(payload):
        with lock:
            print(json.dumps(payload), flush=True)
    qos = QoSProfile(depth=32, durability=DurabilityPolicy.TRANSIENT_LOCAL, reliability=ReliabilityPolicy.RELIABLE)
    latest = {}
    def feedback(msg):
        event = json.loads(msg.data)
        latest.clear()
        latest.update(event)
        emit({'event': event})
    subscription = node.create_subscription(String, '/robot_ai/mission_status', feedback, qos)
    submit = node.create_client(SubmitMission, '/robot_ai/submit_mission')
    speech = node.create_client(AcknowledgePickSpeech, '/robot_ai/acknowledge_pick_speech')
    cancel = node.create_client(Trigger, '/robot_ai/cancel_mission')
    spin = threading.Thread(target=rclpy.spin, args=(node,), daemon=True)
    spin.start()
    try:
        for line in sys.stdin:
            data = {}
            try:
                data = json.loads(line)
                operation = data['operation']
                client = submit if operation == 'submit' else cancel if operation == 'cancel' else speech if operation == 'speech' else None
                if operation == 'cancel' and (latest.get('request_id') != data.get('request_id') or latest.get('state') in {'completed','rejected','needs_clarification','cancelled'}):
                    emit({'id': data['id'], 'result': {'success':False, 'message':'No matching active mission; no cancellation sent.'}})
                    continue
                if client is None:
                    raise ValueError('Unsupported bridge operation')
                if not client.wait_for_service(timeout_sec=3):
                    emit({'id': data['id'], 'result': {'accepted': False, 'code': 'UNAVAILABLE', 'message': 'Mission planner is unavailable; no request was sent.'}})
                    continue
                request = (SubmitMission.Request(request_id=data['request_id'], command=data['command'], speech_required=data.get('speech_required', False)) if operation == 'submit' else
                    AcknowledgePickSpeech.Request(mission_id=data['mission_id'], stage=data['stage'], played=data['played']) if operation == 'speech' else Trigger.Request())
                future = client.call_async(request)
                deadline = time.monotonic() + (7 if operation == 'speech' else 35)
                while not future.done() and time.monotonic() < deadline:
                    time.sleep(.02)
                if not future.done():
                    result = {'code': 'UNKNOWN', 'message': 'Mission outcome is unknown. Do not replay under a new request ID.'}
                else:
                    response = future.result()
                    fields = ('accepted','mission_id','code','message') if operation == 'submit' else ('success','message')
                    result = {field: getattr(response, field) for field in fields}
                emit({'id': data['id'], 'result': result})
            except Exception as error:
                emit({'id': data.get('id'), 'result': {'code': 'UNKNOWN', 'message': str(error)}})
    finally:
        rclpy.shutdown()
        spin.join(timeout=2)
        node.destroy_node()

if __name__ == '__main__':
    main()
