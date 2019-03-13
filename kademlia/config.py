import os

from kademlia.utils import load_from_file


class Config:

    # DHT main properties configs
    K_SIZE = os.getenv('K_SIZE', 20)
    ALPHA = os.getenv('ALPHA', 3)
    VALUES_TO_WAIT = os.getenv('VALUES_TO_WAIT', 20)
    BOOTSTRAP_NODES = [(addr[0], int(addr[1])) for addr in (tuple(node_addr.split(':')) for node_addr in os.getenv('BOOTSTRAP_NODES').split(','))] if os.getenv('BOOTSTRAP_NODES') else None

    PRIVATE_KEY_PATH = os.getenv('PRIVATE_KEY_PATH', 'key.der')
    PUBLIC_KEY_PATH = os.getenv('PUBLIC_KEY_PATH', 'public.der')

    NODE_PRIVATE_KEY = load_from_file(PRIVATE_KEY_PATH)
    NODE_PUBLIC_KEY = load_from_file(PUBLIC_KEY_PATH)

    # Sawtooth properties
    DHT_NAMESPACE = os.getenv('DHT_NAMESPACE', 'eqt.dht_values')
    SAWTOOTH_REST_API_HOST = os.getenv('SAWTOOTH_REST_API_HOST', '127.0.0.1')
    SAWTOOTH_REST_API_PORT = os.getenv('SAWTOOTH_REST_API_PORT', '8008')
    SAWTOOTH_REST_API_URL = f'http://{SAWTOOTH_REST_API_HOST}:{SAWTOOTH_REST_API_PORT}'
