"""Internal endpoint for coco that waits for a given time."""
import asyncio

from .exceptions import InvalidUsage


async def process_post(request: dict):
    """
    Process the POST request.

    Parameters
    ----------
    request : dict
        Needs to contain `seconds : int or float`. TODO: add this to config checks on start-up when
        another endpoint forwards here.
    """
    if "seconds" not in request:
        raise InvalidUsage("No duration in seconds sent.")
    try:
        seconds = float(request["seconds"])
    except Exception:
        raise InvalidUsage("Value for seconds is not a number.")

    await asyncio.sleep(request["seconds"])