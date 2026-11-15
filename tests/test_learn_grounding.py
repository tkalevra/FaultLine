"""Unit tests for the /expand grounding sweep (``_bind_orphans_to_seed``).

Phase 0 of DESIGN-entity-grounding-and-anchoring: orphan entities produced by
``/learn`` hierarchy generation must be bound back to the seed topic so the ingest
graph has no islands unreachable from the topic. Recall traverses the hierarchy
from the topic anchor; an orphaned entity (e.g. ``control node`` defined only
against the descriptive string ``ansible host``) is never reached and degrades to
loose vector similarity. The sweep injects ``{head} part_of {seed}`` per orphan
component. Pure function — no DB, no stack.
"""
from src.api.main import _bind_orphans_to_seed


def test_orphan_island_gets_bound():
    # 'control node' is defined only against descriptive strings — orphaned from 'ansible'.
    edges = [
        {"subject": "control node", "rel_type": "instance_of", "object": "ansible host"},
        {"subject": "control node", "rel_type": "runs", "object": "ansible cli tools"},
    ]
    out, bound = _bind_orphans_to_seed(edges, "ansible")
    assert bound == ["control node"]  # the head entity, not a descriptive object
    assert {
        "subject": "control node", "rel_type": "part_of", "object": "ansible",
        "subject_type": None, "object_type": "Concept",
    } in out


def test_reachable_entity_not_touched():
    # role -> playbook -> ansible and task -> play -> playbook : all reachable, none re-bound.
    edges = [
        {"subject": "playbook", "rel_type": "part_of", "object": "ansible"},
        {"subject": "role", "rel_type": "part_of", "object": "playbook"},
        {"subject": "task", "rel_type": "part_of", "object": "play"},
        {"subject": "play", "rel_type": "part_of", "object": "playbook"},
    ]
    out, bound = _bind_orphans_to_seed(edges, "ansible")
    assert bound == []
    assert out == edges  # nothing injected


def test_ansible_mixed_case():
    edges = [
        # reachable component
        {"subject": "playbook", "rel_type": "part_of", "object": "ansible"},
        {"subject": "role", "rel_type": "part_of", "object": "playbook"},
        # orphan island 1
        {"subject": "control node", "rel_type": "instance_of", "object": "ansible host"},
        # orphan island 2
        {"subject": "ansible inventory", "rel_type": "instance_of", "object": "list"},
        {"subject": "ansible inventory", "rel_type": "contains", "object": "groups"},
    ]
    out, bound = _bind_orphans_to_seed(edges, "ansible")
    assert set(bound) == {"control node", "ansible inventory"}
    # exactly one binding per orphan component; reachable component untouched
    assert len(out) == len(edges) + 2


def test_empty_is_noop():
    out, bound = _bind_orphans_to_seed([], "ansible")
    assert out == []
    assert bound == []


def test_seed_only_edge_is_noop():
    edges = [{"subject": "playbook", "rel_type": "part_of", "object": "ansible"}]
    out, bound = _bind_orphans_to_seed(edges, "ansible")
    assert bound == []
    assert out == edges


def test_representative_is_highest_subject_degree():
    # 'inventory' is the subject twice; 'list'/'groups' are objects only -> 'inventory' is the head.
    edges = [
        {"subject": "inventory", "rel_type": "instance_of", "object": "list"},
        {"subject": "inventory", "rel_type": "contains", "object": "groups"},
    ]
    _out, bound = _bind_orphans_to_seed(edges, "ansible")
    assert bound == ["inventory"]


def test_binding_rel_param_respected():
    edges = [{"subject": "control node", "rel_type": "instance_of", "object": "ansible host"}]
    out, _bound = _bind_orphans_to_seed(edges, "ansible", binding_rel="related_to")
    injected = [e for e in out if e not in edges]
    assert injected and injected[0]["rel_type"] == "related_to"


def test_case_insensitive_seed_match():
    # 'Playbook' already connects to 'Ansible' once case-normalized -> not an orphan.
    edges = [{"subject": "Playbook", "rel_type": "part_of", "object": "Ansible"}]
    _out, bound = _bind_orphans_to_seed(edges, "ANSIBLE")
    assert bound == []


def test_subject_type_carried_into_binding():
    edges = [{"subject": "control node", "rel_type": "instance_of",
              "object": "ansible host", "subject_type": "Object"}]
    out, _bound = _bind_orphans_to_seed(edges, "ansible")
    inj = next(e for e in out if e["subject"] == "control node" and e["object"] == "ansible")
    assert inj["subject_type"] == "Object"
    assert inj["object_type"] == "Concept"
