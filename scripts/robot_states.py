"""ROS-independent status definitions shared by the display and voice agent."""
STATES = ('IDLE', 'LISTENING', 'THINKING', 'SPEAKING', 'ERROR', 'SLEEPING', 'WORKING', 'DETECTING')
INPUT_STATES = frozenset(('IDLE', 'LISTENING'))
ANIMATIONS = dict(IDLE='idle', LISTENING='needs-input', THINKING='thinking',
                  SPEAKING='speaking', ERROR='error', SLEEPING='sleeping',
                  WORKING='working', DETECTING='searching')


def normalize_status(value):
    status = value.strip().upper() if isinstance(value, str) else ''
    if status not in STATES:
        raise ValueError('Status must be one of: ' + ', '.join(STATES))
    return status
