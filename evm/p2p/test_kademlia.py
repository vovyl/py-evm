import asyncio
import random

import pytest

from evm.p2p import kademlia
from evm.utils.numeric import int_to_big_endian


# Force our tests to fail quickly if they accidentally make network requests.
@pytest.fixture(autouse=True)
def short_timeout(monkeypatch):
    monkeypatch.setattr(kademlia, 'k_request_timeout', 0.01)


@pytest.mark.asyncio
def test_protocol_bootstrap():
    proto = get_wired_protocol()
    node1, node2 = [random_node(), random_node()]

    @asyncio.coroutine
    def bond(node):
        assert proto.routing.add_node(node) is None
        return True

    # Pretend we bonded successfully with our bootstrap nodes.
    proto.bond = bond

    yield from proto.bootstrap([node1, node2])

    assert len(proto.wire.messages) == 2
    # We don't care in which order the bootstrap nodes are contacted, so we sort them both in the
    # assert here.
    assert sorted(proto.wire.messages) == sorted([
        (node1, 'find_node', proto.routing.this_node.id),
        (node2, 'find_node', proto.routing.this_node.id)])


@pytest.mark.asyncio
def test_wait_ping():
    proto = get_wired_protocol()
    node = random_node()
    echo = "echo"

    # Schedule a call to proto.recv_ping() simulating a ping from the node we expect.
    recv_ping_coroutine = asyncio.coroutine(lambda: proto.recv_ping(node, echo))
    asyncio.ensure_future(recv_ping_coroutine())

    got_ping = yield from proto.wait_ping(node)

    assert got_ping
    # Ensure wait_ping() cleaned up after itself.
    assert node not in proto.ping_callbacks

    # If we waited for a ping from a different node, wait_ping() would timeout and thus return
    # false.
    recv_ping_coroutine = asyncio.coroutine(lambda: proto.recv_ping(node, echo))
    asyncio.ensure_future(recv_ping_coroutine())

    node2 = random_node()
    got_ping = yield from proto.wait_ping(node2)

    assert not got_ping
    assert node2 not in proto.ping_callbacks


@pytest.mark.asyncio
def test_wait_pong():
    proto = get_wired_protocol()
    node = random_node()
    echoed = "echoed"
    pingid = proto._mkpingid(echoed, node)

    # Schedule a call to proto.recv_pong() simulating a pong from the node we expect.
    recv_pong_coroutine = asyncio.coroutine(lambda: proto.recv_pong(node, echoed))
    asyncio.ensure_future(recv_pong_coroutine())

    got_pong = yield from proto.wait_pong(pingid)

    assert got_pong
    # Ensure wait_pong() cleaned up after itself.
    assert pingid not in proto.pong_callbacks

    # If the remote node echoed something different than what we expected, wait_pong() would
    # timeout.
    wrong_echo = "foo"
    recv_pong_coroutine = asyncio.coroutine(lambda: proto.recv_pong(node, wrong_echo))
    asyncio.ensure_future(recv_pong_coroutine())

    got_pong = yield from proto.wait_pong(pingid)

    assert not got_pong
    assert pingid not in proto.pong_callbacks


@pytest.mark.asyncio
def test_wait_neighbours():
    proto = get_wired_protocol()
    node = random_node()

    # Schedule a call to proto.recv_neighbours() simulating a neighbours response from the node we
    # expect.
    neighbours = [random_node(), random_node(), random_node()]
    recv_neighbours_coroutine = asyncio.coroutine(lambda: proto.recv_neighbours(node, neighbours))
    asyncio.ensure_future(recv_neighbours_coroutine())

    received_neighbours = yield from proto.wait_neighbours(node)

    assert neighbours == received_neighbours
    # Ensure wait_neighbours() cleaned up after itself.
    assert node not in proto.neighbours_callbacks

    # If wait_neighbours() times out, we get an empty list of neighbours.
    received_neighbours = yield from proto.wait_neighbours(node)

    assert received_neighbours == []
    assert node not in proto.neighbours_callbacks


def test_routingtable_split_bucket():
    table = kademlia.RoutingTable(random_node())
    assert len(table.buckets) == 1
    old_bucket = table.buckets[0]
    table.split_bucket(0)
    assert len(table.buckets) == 2
    assert old_bucket not in table.buckets


def test_routingtable_add_node():
    table = kademlia.RoutingTable(random_node())
    for i in range(table.buckets[0].k):
        # As long as the bucket is not full, the new node is added to the bucket and None is
        # returned.
        assert table.add_node(random_node()) is None
        assert len(table.buckets) == 1
        assert len(table) == i + 1
    assert table.buckets[0].is_full
    # Now that the bucket is full, an add_node() should cause it to be split.
    assert table.add_node(random_node()) is None


def test_routingtable_add_node_error():
    table = kademlia.RoutingTable(random_node())
    with pytest.raises(ValueError):
        table.add_node(random_node(kademlia.k_max_node_id + 1))


def test_routingtable_neighbours():
    table = kademlia.RoutingTable(random_node())
    for i in range(1000):
        assert table.add_node(random_node()) is None
    assert i == len(table) - 1

    for i in range(100):
        node = random_node()
        nearest_bucket = table.buckets_by_distance_to(node.id)[0]
        if not nearest_bucket.nodes:
            continue
        # Change nodeid to something in this bucket.
        node_a = nearest_bucket.nodes[0]
        node_b = random_node(node_a.id + 1)
        assert node_a == table.neighbours(node_b.id)[0]


def test_kbucket_add():
    bucket = kademlia.KBucket(0, 100)
    node = random_node()
    assert bucket.add(node) is None
    assert bucket.nodes == [node]

    node2 = random_node()
    assert bucket.add(node2) is None
    assert bucket.nodes == [node, node2]
    assert bucket.head == node

    assert bucket.add(node) is None
    assert bucket.nodes == [node2, node]
    assert bucket.head == node2

    bucket.k = 2
    node3 = random_node()
    assert bucket.add(node3) == node2
    assert bucket.nodes == [node2, node]
    assert bucket.head == node2


def test_kbucket_split():
    bucket = kademlia.KBucket(0, 100)
    for i in range(1, bucket.k + 1):
        node = random_node()
        # Set the IDs of half the nodes below the midpoint, so when we split we should end up with
        # two buckets containing k/2 nodes.
        if i % 2 == 0:
            node.id = bucket.midpoint + i
        else:
            node.id = bucket.midpoint - i
        bucket.add(node)
    assert bucket.is_full
    bucket1, bucket2 = bucket.split()
    assert bucket1.start == 0
    assert bucket1.end == 50
    assert bucket2.start == 51
    assert bucket2.end == 100
    assert len(bucket1) == bucket.k / 2
    assert len(bucket2) == bucket.k / 2


def test_compute_shared_prefix_bits():
    # When we have less than 2 nodes, the depth is k_id_size.
    nodes = [random_node()]
    assert kademlia.compute_shared_prefix_bits(nodes) == kademlia.k_id_size

    # Otherwise the depth is the number of leading bits (in the left-padded binary representation)
    # shared by all node IDs.
    nodes.append(random_node())
    nodes[0].id = int('0b1', 2)
    nodes[1].id = int('0b0', 2)
    assert kademlia.compute_shared_prefix_bits(nodes) == kademlia.k_id_size - 1

    nodes[0].id = int('0b010', 2)
    nodes[1].id = int('0b110', 2)
    assert kademlia.compute_shared_prefix_bits(nodes) == kademlia.k_id_size - 3


def get_wired_protocol():
    this_node = random_node()
    return kademlia.KademliaProtocol(this_node, WireMock(this_node))


def random_pubkey():
    pk = int_to_big_endian(random.getrandbits(kademlia.k_pubkey_size))
    return b'\x00' * (kademlia.k_pubkey_size // 8 - len(pk)) + pk


def random_node(nodeid=None):
    address = kademlia.Address('127.0.0.1', 30303)
    node = kademlia.Node(random_pubkey(), address)
    if nodeid is not None:
        node.id = nodeid
    return node


def make_routing_table(num_nodes=1000):
    node = random_node()
    table = kademlia.RoutingTable(node)
    for i in range(num_nodes):
        table.add_node(random_node())
    assert i == num_nodes - 1
    return table


class WireMock():

    messages = []

    def __init__(self, sender):
        self.sender = sender

    def send_ping(self, node):
        echo = hex(random.randint(0, 2**256))[-32:]
        self.messages.append((node, 'ping', echo))
        return echo

    def send_pong(self, node, echo):
        self.messages.append((node, 'pong', echo))

    def send_find_node(self, node, nodeid):
        self.messages.append((node, 'find_node', nodeid))

    def send_neighbours(self, node, neighbours):
        self.messages.append((node, 'neighbours', neighbours))
