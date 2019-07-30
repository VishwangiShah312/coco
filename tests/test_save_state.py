"""Test endpoint config option `save_state` and `get_state`."""
import pytest

from coco.test import coco_runner

SAVE_ENDPT_NAME = "save"
GET_ENDPT_NAME1 = "get1"
GET_ENDPT_NAME2 = "get2"

CONFIG = {"log_level": "INFO", "groups": {"no_group": ["no_host", "doesnt_exist"]}}
INT_VAL = 5
INT_VAL_NAME = "val"
STATE_PATH = "test_state"
ENDPOINTS = {
    SAVE_ENDPT_NAME: {
        "call": {"forward": None},
        "save_state": [STATE_PATH + "/1", STATE_PATH + "/2"],
        "values": {INT_VAL_NAME: "int"},
    },
    GET_ENDPT_NAME1: {"call": {"forward": None}, "get_state": STATE_PATH + "/1"},
    GET_ENDPT_NAME2: {"call": {"forward": None}, "get_state": STATE_PATH + "/2"},
}


@pytest.fixture
def runner():
    """Create a coco runner."""
    return coco_runner.Runner(CONFIG, ENDPOINTS)


def test_save_state(runner):
    """Test get/save_state."""
    # State should be empty now
    response = runner.client(GET_ENDPT_NAME1)
    assert "state" in response
    assert STATE_PATH in response["state"]
    assert "1" in response["state"][STATE_PATH]
    assert response["state"][STATE_PATH]["1"] == {}
    response = runner.client(GET_ENDPT_NAME2)
    assert "state" in response
    assert STATE_PATH in response["state"]
    assert "2" in response["state"][STATE_PATH]
    assert response["state"][STATE_PATH]["2"] == {}

    # Set state to INT_VAL
    runner.client(SAVE_ENDPT_NAME, {INT_VAL_NAME: INT_VAL})
    response = runner.client(GET_ENDPT_NAME1)
    assert "state" in response
    assert STATE_PATH in response["state"]
    assert "1" in response["state"][STATE_PATH]
    assert response["state"][STATE_PATH]["1"] == {INT_VAL_NAME: INT_VAL}
    response = runner.client(GET_ENDPT_NAME2)
    assert "state" in response
    assert STATE_PATH in response["state"]
    assert "2" in response["state"][STATE_PATH]
    assert response["state"][STATE_PATH]["2"] == {INT_VAL_NAME: INT_VAL}
