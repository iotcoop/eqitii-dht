"""
Package for interacting on the network at a high level.
"""
import json
import random
import pickle
import asyncio
import logging

from kademlia.config import Config
from kademlia.domain.domain import Value, NodeMessage, ControlledValue, ValueFactory, validate_secure_value, \
    select_most_common_response
from kademlia.exceptions import UnauthorizedOperationException
from kademlia.protocol import KademliaProtocol
from kademlia.repository import ValidatorRepository, from_dtl, compose_url
from kademlia.utils import digest, digest256
from kademlia.storage import ForgetfulStorage
from kademlia.node import Node
from kademlia.crawling import ValueSpiderCrawl
from kademlia.crawling import NodeSpiderCrawl

log = logging.getLogger(__name__)
validatorRepository = ValidatorRepository(from_dtl(compose_url(Config.SAWTOOTH_REST_API_URL, 'state'))(Config.DHT_NAMESPACE))


class Server(object):
    """
    High level view of a node instance.  This is the object that should be
    created to start listening as an active node on the network.
    """

    protocol_class = KademliaProtocol

    def __init__(self, ksize=20, alpha=3, node_id=None, storage=None):
        """
        Create a server instance.  This will start listening on the given port.

        Args:
            ksize (int): The k parameter from the paper
            alpha (int): The alpha parameter from the paper
            node_id: The id for this node on the network.
            storage: An instance that implements
                     :interface:`~kademlia.storage.IStorage`
        """
        self.ksize = ksize
        self.alpha = alpha
        self.storage = storage or ForgetfulStorage()
        self.node = Node(node_id or digest(random.getrandbits(255)))
        self.transport = None
        self.protocol = None
        self.refresh_loop = None
        self.save_state_loop = None

    def stop(self):
        if self.transport is not None:
            self.transport.close()

        if self.refresh_loop:
            self.refresh_loop.cancel()

        if self.save_state_loop:
            self.save_state_loop.cancel()

    def _create_protocol(self):
        return self.protocol_class(self.node, self.storage, self.ksize)

    def listen(self, port, interface='0.0.0.0'):
        """
        Start listening on the given port.

        Provide interface="::" to accept ipv6 address
        """
        loop = asyncio.get_event_loop()
        listen = loop.create_datagram_endpoint(self._create_protocol,
                                               local_addr=(interface, port))
        log.info("Node %i listening on %s:%i",
                 self.node.long_id, interface, port)
        self.transport, self.protocol = loop.run_until_complete(listen)
        # finally, schedule refreshing table
        self.refresh_table()

    def refresh_table(self):
        log.debug("Refreshing routing table")
        asyncio.ensure_future(self._refresh_table())
        loop = asyncio.get_event_loop()
        self.refresh_loop = loop.call_later(3600, self.refresh_table)

    async def _refresh_table(self):
        """
        Refresh buckets that haven't had any lookups in the last hour
        (per section 2.3 of the paper).
        """
        ds = []
        for node_id in self.protocol.getRefreshIDs():
            node = Node(node_id)
            nearest = self.protocol.router.findNeighbors(node, self.alpha)
            spider = NodeSpiderCrawl(self.protocol, node, nearest,
                                     self.ksize, self.alpha)
            ds.append(spider.find())

        # do our crawling
        await asyncio.gather(*ds)

        # now republish keys older than one hour
        for dkey, value in self.storage.iteritemsOlderThan(3600):
            values_to_republish = []
            parsed_val = json.loads(value)
            if isinstance(parsed_val, list):
                [values_to_republish.append(json.dumps(val)) for val in parsed_val]
            else:
                values_to_republish.append(value)

            for val in values_to_republish:
                await self._call_remote_persist(dkey, val)

    def bootstrappableNeighbors(self):
        """
        Get a :class:`list` of (ip, port) :class:`tuple` pairs suitable for
        use as an argument to the bootstrap method.

        The server should have been bootstrapped
        already - this is just a utility for getting some neighbors and then
        storing them if this server is going down for a while.  When it comes
        back up, the list of nodes can be used to bootstrap.
        """
        neighbors = self.protocol.router.findNeighbors(self.node)
        return [tuple(n)[-2:] for n in neighbors]

    async def bootstrap(self, addrs):
        """
        Bootstrap the server by connecting to other known nodes in the network.

        Args:
            addrs: A `list` of (ip, port) `tuple` pairs.  Note that only IP
                   addresses are acceptable - hostnames will cause an error.
        """
        log.debug("Attempting to bootstrap node with %i initial contacts",
                  len(addrs))
        cos = list(map(self.bootstrap_node, addrs))
        gathered = await asyncio.gather(*cos)
        nodes = [node for node in gathered if node is not None]
        spider = NodeSpiderCrawl(self.protocol, self.node, nodes,
                                 self.ksize, self.alpha)
        return await spider.find()

    async def bootstrap_node(self, addr):
        result = await self.protocol.ping(addr, self.node.id)
        return Node(result[1], addr[0], addr[1]) if result[0] else None

    async def get(self, key):
        """
        Get a key if the network has it.

        Returns:
            :class:`None` if not found, the value otherwise.
        """
        log.info("Looking up key %s", key)
        dkey = digest(key)
        # if this node has it, return it

        node = Node(dkey)
        nearest = self.protocol.router.findNeighbors(node)
        if len(nearest) == 0:
            log.warning("There are no known neighbors to get key %s", key)
            return NodeMessage.of_params(dkey, None)
        spider = ValueSpiderCrawl(self.protocol, node, nearest,
                                  self.ksize, self.alpha)

        local_value = self.storage.get(dkey, None)

        if local_value:
            local_value = NodeMessage.of_params(dkey, local_value).to_json()
            responses = await spider.find([local_value])
        else:
            responses = await spider.find()

        most_common_response = select_most_common_response(dkey, responses)

        return NodeMessage.of_params(dkey, most_common_response)

    async def set(self, key, new_value: Value):
        """
         Set the given string key to the given value in the network.
        """

        log.info(f"Going to set {key} = {new_value} on network")
        log.debug("Going to process save request")

        if not new_value.is_valid():
            raise UnauthorizedOperationException()
        await self._persist_locally(key, new_value)
        return await self._call_remote_persist(key, str(new_value))

    async def _persist_locally(self, key, new_value: Value):
        """
        Validate and persist new value locally
        :param key: plain value key
        :param new_value: new value to persist on the network
        """
        dkey = digest(key)

        log.debug(f"Going to retrieve stored value for key: {dkey}")
        value_response = await self.get(key)

        if not self._get_dtl_record(dkey, new_value):
            raise UnauthorizedOperationException()

        if value_response.data:
            stored_value = ValueFactory.create_from_string(dkey, value_response.data)
            if isinstance(stored_value, ControlledValue):
                result = stored_value.add_value(new_value)
            else:
                validate_secure_value(dkey, new_value, stored_value)
                result = new_value
        else:
            result = ValueFactory.create_from_value(new_value)

        if not self._get_dtl_record(dkey, new_value):
            raise UnauthorizedOperationException()

        self.storage[dkey] = str(result)

    @staticmethod
    def _get_dtl_record(dkey, new_value):
        val_sign = new_value.authorization.sign
        value__hash = digest256(dkey.hex() + val_sign).hex()
        return validatorRepository.get_by_id(value__hash)

    async def _call_remote_persist(self, key, value: str):
        """
        Set the given SHA1 digest key (bytes) to the given value in the
        network.
        """
        dkey = digest(key)
        node = Node(dkey)

        nearest = self.protocol.router.findNeighbors(node)
        if len(nearest) == 0:
            log.warning("There are no known neighbors to set key %s",
                        dkey.hex())
            return False

        spider = NodeSpiderCrawl(self.protocol, node, nearest,
                                 self.ksize, self.alpha)
        nodes = await spider.find()
        log.info("setting '%s' on %s", dkey.hex(), list(map(str, nodes)))

        ds = [self.protocol.callStore(n, dkey, value) for n in nodes]
        # return true only if at least one store call succeeded
        return any(await asyncio.gather(*ds))

    def saveState(self, fname):
        """
        Save the state of this node (the alpha/ksize/id/immediate neighbors)
        to a cache file with the given fname.
        """
        log.info("Saving state to %s", fname)
        data = {
            'ksize': self.ksize,
            'alpha': self.alpha,
            'id': self.node.id,
            'neighbors': self.bootstrappableNeighbors()
        }
        if len(data['neighbors']) == 0:
            log.warning("No known neighbors, so not writing to cache.")
            return
        with open(fname, 'wb') as f:
            pickle.dump(data, f)

    @classmethod
    def loadState(self, fname):
        """
        Load the state of this node (the alpha/ksize/id/immediate neighbors)
        from a cache file with the given fname.
        """
        log.info("Loading state from %s", fname)
        with open(fname, 'rb') as f:
            data = pickle.load(f)
        s = Server(data['ksize'], data['alpha'], data['id'])
        if len(data['neighbors']) > 0:
            s.bootstrap(data['neighbors'])
        return s

    def saveStateRegularly(self, fname, frequency=600):
        """
        Save the state of node with a given regularity to the given
        filename.

        Args:
            fname: File name to save retularly to
            frequency: Frequency in seconds that the state should be saved.
                        By default, 10 minutes.
        """
        self.saveState(fname)
        loop = asyncio.get_event_loop()
        self.save_state_loop = loop.call_later(frequency,
                                               self.saveStateRegularly,
                                               fname,
                                               frequency)


def check_dht_value_type(value):
    """
    Checks to see if the type of the value is a valid type for
    placing in the dht.
    """
    typeset = set(
        [
            int,
            float,
            bool,
            str,
            bytes,
        ]
    )
    return type(value) in typeset

