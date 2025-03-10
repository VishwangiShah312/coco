"""The host blocklist."""

import os
import logging
from typing import List, Tuple, Iterable

from .util import Host, PersistentState
from .exceptions import InvalidUsage
from .result import Result

# Get a logging object
logger = logging.getLogger(__name__)


class Blocklist:
    """Hold the state of the node blocklist.

    Can fetch, update and persist the blocklist to/from disk.

    Parameters
    ----------
    path
        Path to store the blocklist.
    """

    def __init__(self, hosts, path: os.PathLike):

        # Initialise persistent storage
        self._state = PersistentState(path)
        if self._state.state is None:
            with self._state.update():
                self._state.state = {"blocklist_hosts": []}
        self._build_hosts()

        self._known_hosts = set()
        self._known_hosts_dict = {}
        self.add_known_hosts(hosts)

        # Set the commands that can be executed via the endpoint
        self._commands = {
            "add": self.add_hosts,
            "remove": self.remove_hosts,
            "clear": self.clear_hosts,
        }

    def add_hosts(self, hosts: List[str]) -> bool:
        """Add the hosts to the blocklist.

        If any hosts are not known the whole update is rejected.

        Parameters
        ----------
        hosts
            List of hosts to add (as `host:port`).

        Returns
        -------
        success
            Did the update succeed.
        """
        h, checks = self._check_hosts(hosts)

        if not all(checks):
            bad_hosts = [host for host, check in zip(hosts, checks) if not check]
            msg = f"Could not add to blocklist. Requested hosts {bad_hosts} unknown."
            logger.debug(msg)  # TODO: should this be logged here?

            raise InvalidUsage(
                "Could not add to blocklists as some hosts unknown.", context=bad_hosts
            )

        hosts = set(h)

        already_blocklisted = hosts & self.hosts
        if already_blocklisted:
            logger.debug(
                f"Hosts {Host.print_list(already_blocklisted)} are already blocklisted."
            )
        hosts -= already_blocklisted

        if not hosts:
            logger.debug("Nothing to add.")
            return True

        new_hosts = [f"{host}" for host in self.hosts | hosts]

        logger.info(f"Adding {Host.print_list(hosts)} to blocklist.")
        with self._state.update():
            logger.info(new_hosts)
            self._state.state["blocklist_hosts"] = new_hosts
        self._build_hosts()

        return True

    def remove_hosts(self, hosts: List[str]) -> bool:
        """Remove the hosts from the blocklist.

        If any hosts are not known the whole update is rejected.

        Parameters
        ----------
        hosts
            List of hosts to remove (as `host:port`)

        Returns
        -------
        success
            Did the update succeed.
        """
        h, checks = self._check_hosts(hosts)

        if not all(checks):
            bad_hosts = [host for host, check in zip(hosts, checks) if not check]
            msg = (
                f"Could not remove from blocklist. Requested hosts {bad_hosts} unknown."
            )
            logger.debug(msg)  # TODO: should this be logged here?

            raise InvalidUsage(
                "Could not remove from blocklist as some hosts unknown.",
                context=bad_hosts,
            )

        hosts = set(h)

        not_blocklisted = hosts - self.hosts
        if not_blocklisted:
            logger.debug(
                f"Hosts {Host.print_list(not_blocklisted)} are not in " "the blocklist."
            )
        hosts -= not_blocklisted

        if not hosts:
            logger.debug("Nothing to remove.")
            return True

        new_hosts = [f"{host}" for host in set(self.hosts) - set(hosts)]

        logger.info(f"Removing {Host.print_list(hosts)} from blocklist.")
        with self._state.update():
            self._state.state["blocklist_hosts"] = new_hosts
        self._build_hosts()

        return True

    def clear_hosts(self, *_) -> bool:
        """Clear the blocklist.

        Returns
        -------
        success
            Did the update succeed.
        """
        with self._state.update():
            self._state.state["blocklist_hosts"] = []
        self._build_hosts()

        return True

    def _check_hosts(self, hosts: List[Host]) -> Tuple[List[Host], List[bool]]:
        """Check hosts against list of known hosts.

        Hosts with a missing port will have port filled in *if and only if*
        there is a single candidate amongst the known hosts.

        Parameters
        ----------
        hosts
            Hosts to check.

        Returns
        -------
        hosts
            List of hosts with any subsitutions.
        checks
            Whether the given host had a valid match.
        """

        def _check_host(host):

            if not isinstance(host, Host):
                host = Host(host)

            valid = True

            # First check to see if any hosts match the hostname
            if host.hostname not in self._known_hosts_dict:
                logger.debug(f"No known host with matching hostname={host.hostname}")
                valid = False

            # If no port was set, try and find a matching one...
            elif host.port is None:
                matching_hosts = self._known_hosts_dict[host.hostname]

                if len(matching_hosts) > 1:
                    logger.debug(
                        f"Cannot match hostname={host.hostname} to a unique "
                        f"host:port combination ({len(matching_hosts)} possibilities)."
                    )
                    valid = False
                elif len(matching_hosts) == 0:
                    logger.debug(f"No host matching hostname={host.hostname} found")
                    valid = False
                host = list(matching_hosts)[0]
            # Check if there are any matching host+port entries
            elif host not in self._known_hosts_dict[host.hostname]:
                logger.debug(
                    f"Hosts found with matching hostname={host.hostname}, "
                    "but none have port={host.port}"
                )
                valid = False

            return host, valid

        # Check and substitute the host list
        if not hosts:
            return [], [True]
        return zip(*[_check_host(host) for host in hosts])

    def _build_hosts(self):
        """Cache the list of hosts from the state."""
        self._hosts = set(
            Host(hoststr) for hoststr in self._state.state["blocklist_hosts"]
        )

    @property
    def hosts(self) -> List[Host]:
        """Get the blocklisted hosts.

        Returns
        -------
        hosts
            List of blocklisted hosts.
        """
        return self._hosts

    def add_known_hosts(self, hosts: Iterable[Host]):
        """Add to the set of known hosts.

        Parameters
        ----------
        hosts
            List of known hosts.
        """

        self._known_hosts.update(hosts)
        # Make a lookup table for hosts by their hostname
        for host in hosts:
            self._known_hosts_dict.setdefault(host.hostname, set()).add(host)

    async def process_get(self, _):
        """Process the GET request."""
        return Result(
            "blocklist",
            result={Host("coco"): ([f"{host}" for host in self.hosts], 200)},
            type_="FULL",
        )

    async def process_post(self, request: dict):
        """Process the POST request."""
        if "command" not in request:
            raise InvalidUsage("No blocklist command sent.")

        command = request["command"]

        if command not in self._commands:
            raise InvalidUsage(
                f"Unknown command {command}. Supported commands "
                f"are {self._commands.keys()}"
            )

        hosts = request.get("hosts", None)
        self._commands[command](hosts)
