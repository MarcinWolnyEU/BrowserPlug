"""identity_claim.claim_verdict: who may take a live browser record over."""

from types import SimpleNamespace

from server.ws.identity_claim import claim_verdict


def rec(label="", remote=False, addr=None, pid=None):
    return SimpleNamespace(device_label=label, is_remote=remote, remote_addr=addr,
                           browser_pid=pid)


def test_same_device_label_matches_and_different_labels_never_do():
    assert claim_verdict(rec("a", True, "1.1.1.1"), rec("a", True, "2.2.2.2"))[0]
    assert not claim_verdict(rec("a", True, "1.1.1.1"), rec("b", True, "1.1.1.1"))[0]
    assert not claim_verdict(rec("a", pid=5), rec("b", pid=5))[0]


def test_local_peers_need_the_same_known_pid():
    assert claim_verdict(rec(pid=5), rec(pid=5))[0]
    assert not claim_verdict(rec(pid=5), rec(pid=6))[0]
    assert not claim_verdict(rec(), rec())[0]
    assert not claim_verdict(rec(pid=5), rec())[0]


def test_local_and_remote_never_match():
    assert not claim_verdict(rec(pid=5), rec(remote=True, addr="1.1.1.1"))[0]
    assert not claim_verdict(rec(remote=True, addr="1.1.1.1"), rec(pid=5))[0]


def test_remote_peers_without_labels_fall_back_to_the_address():
    assert claim_verdict(rec(remote=True, addr="1.1.1.1"), rec(remote=True, addr="1.1.1.1"))[0]
    assert not claim_verdict(rec(remote=True, addr="1.1.1.1"), rec(remote=True, addr="2.2.2.2"))[0]
    assert not claim_verdict(rec("a", True, "1.1.1.1"), rec("", True, "1.1.1.1"))[0]


def test_a_refusal_says_why():
    assert claim_verdict(rec(pid=5), rec(pid=6))[1]
