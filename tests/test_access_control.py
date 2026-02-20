from src.domain.value_objects import UserId
from src.services.access_control import AccessControlList, InMemoryAccessStore


def test_access_control_grant_and_revoke():
    acl = AccessControlList(InMemoryAccessStore())
    user_id = UserId("123")

    assert acl.is_allowed(user_id) is False

    acl.grant(user_id)
    assert acl.is_allowed(user_id) is True

    acl.revoke(user_id)
    assert acl.is_allowed(user_id) is False
