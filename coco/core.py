"""
coco core module.

This is the core of coco. Endpoints are loaded and called through the core module.
Also loads the config.
"""
import asyncio
import datetime
import logging
import time
import os
from pathlib import Path
from multiprocessing import Process, set_start_method

import json
import redis
import aioredis

from sanic import Sanic, response

from comet import Manager, CometError

from .request_forwarder import (
    CocoForward,
    RequestForwarder,
)
from .endpoint import (
    Endpoint,
    LocalEndpoint,
)
from . import worker, __version__, wait
from .state import State
from .exceptions import ConfigError, InternalError
from .util import Host, str2total_seconds
from . import slack
from . import config

logger = logging.getLogger(__name__)

# This should be a no-op on Linux but is required on MacOS for coco to run
set_start_method("fork")


class Core:
    """
    The core module.

    Loads and keeps the config and endpoints. Endpoints are called through this module.
    """

    def __init__(self, conf, reset=False, check_config=False):
        """
        Coco Core.

        Parameters
        ----------
        conf : os.PathLike
            Path to the config file.
        reset : bool
            Whether to reset internal state on start. Default `False`.
        check_config : bool
            Don't really start, check config only. Default `False`.
        """

        # Tell the destructor that there's no worker to be killed
        self.check_config = check_config

        # In case constructor crashes before this gets assigned, so that destructor
        # doesn't fail.
        self.qworker = None
        self.state = None

        # Load the config
        self._load_config(Path(conf))
        logger.setLevel(self.config["log_level"])

        if reset is True:
            # Reset the internal state
            asyncio.run(self.state.reset_state())

        # Configure the forwarder
        try:
            timeout = str2total_seconds(self.config["timeout"])
        except Exception as e:
            raise ConfigError(
                f"Failed parsing value 'timeout' ({self.config['timeout']})."
            ) from e
        self.forwarder = RequestForwarder(
            self.blocklist_path,
            timeout,
            debug_connections=self.config["debug_connections"],
        )
        self.forwarder.set_session_limit(self.config["session_limit"])
        for group, hosts in self.groups.items():
            self.forwarder.add_group(group, hosts)

        self._config_slack_loggers()

        self._load_endpoints()
        self._local_endpoints()
        self._check_endpoint_links()
        self._register_config()

        try:
            self.frontend_timeout = str2total_seconds(self.config["frontend_timeout"])
        except Exception as e:
            raise ConfigError(
                "Failed parsing value 'frontend_timeout' "
                f"({self.config['frontend_timeout']})."
            ) from e

        if self.check_config:
            logger.info("Superficial config check successful. Stopping...")
            return

        # Remove any leftover shutdown commands from the queue
        self.redis_sync = redis.Redis()
        self.redis_sync.lrem("queue", 0, "coco_shutdown")

        # Load queue update script into redis cache
        self.queue_sha = self.redis_sync.script_load(
            """ if redis.call('llen', KEYS[1]) >= tonumber(ARGV[1]) then
                        return true
                    else
                        redis.call('hmset', KEYS[2], ARGV[2], ARGV[3], ARGV[4], ARGV[5], ARGV[6], ARGV[7], ARGV[8], ARGV[9], ARGV[10], ARGV[11])
                        redis.call('rpush', KEYS[1], KEYS[2])
                        return false
                    end
            """
        )

        # Start the worker process
        self.qworker = Process(
            target=worker.main_loop,
            args=(
                self.endpoints,
                self.forwarder,
                self.config["port"],
                self.config["metrics_port"],
                self.config["log_level"],
                self.frontend_timeout,
            ),
        )
        self.qworker.daemon = True
        try:
            self.qworker.start()
        except Exception:
            self.qworker.join()

        self._call_endpoints_on_start()
        self._start_server()

        self.redis_async = None

    def __del__(self):
        """
        Destruct :class:`Core`.

        Join the worker process.
        """
        if not self.check_config:
            logger.info("Joining worker process...")
            try:
                self.redis_sync.rpush("queue", "coco_shutdown")
            except Exception as e:
                logger.error(
                    f"Failed sending shutdown command to worker (have to kill it): {type(e)}: {e}"
                )
                self._kill_worker()
            self._kill_worker()

    def _kill_worker(self):
        if hasattr(self, "qworker"):
            if self.qworker:
                self.qworker.kill()

    def _call_endpoints_on_start(self):
        for endpoint in self.endpoints.values():
            # Initialise request counter
            self.redis_sync.incr(f"dropped_counter_{endpoint.name}", amount=0)
            if endpoint.call_on_start:
                logger.debug(f"Calling endpoint on start: /{endpoint.name}")
                name = f"{os.getpid()}-{time.time()}"

                self.redis_sync.hmset(
                    name,
                    {
                        "method": endpoint.type,
                        "endpoint": endpoint.name,
                        "request": json.dumps({}),
                    },
                )

                # Add task name to queue
                self.redis_sync.rpush("queue", name)

                # Wait for the result
                result = self.redis_sync.blpop(f"{name}:res")[1]
                self.redis_sync.delete(f"{name}:res")
                # TODO: raise log level in failure case?
                logger.debug(f"Called /{endpoint.name} on start, result: {result}")

    def _start_server(self):
        """Start a sanic server."""

        self.sanic_app = Sanic(__name__)
        self.sanic_app.config.REQUEST_TIMEOUT = self.frontend_timeout
        self.sanic_app.config.RESPONSE_TIMEOUT = self.frontend_timeout

        # Create the Redis connection pool, use sanic to start it so that it
        # ends up in the same event loop
        async def init_redis_async(*_):
            self.redis_async = await aioredis.create_redis_pool(
                ("127.0.0.1", 6379), minsize=3, maxsize=10
            )

        async def close_redis_async(*_):
            self.redis_async.close()
            await self.redis_async.wait_closed()

        self.sanic_app.register_listener(init_redis_async, "before_server_start")
        self.sanic_app.register_listener(close_redis_async, "after_server_stop")

        # Set up slack logging, needs to be done here so it gets setup in the right event loop
        def start_slack_log(_, loop):
            slack.start(loop)

        async def stop_slack_log(*_):
            await slack.stop()

        self.sanic_app.register_listener(start_slack_log, "before_server_start")
        self.sanic_app.register_listener(stop_slack_log, "after_server_stop")

        debug = self.log_level == "DEBUG"

        self.sanic_app.add_route(
            self.external_endpoint, "/<endpoint>", methods=["GET", "POST"]
        )

        self.sanic_app.run(
            host="0.0.0.0",
            port=self.config["port"],
            workers=self.config["n_workers"],
            debug=False,
            access_log=debug,
        )

    def _config_slack_loggers(self):
        # Configure the log handlers for posting to slack

        # Don't set up extra loggers if they're not enabled
        if self.config["slack_token"] is None:
            logger.warning(
                "Config variable 'slack_token' not found. Slack messaging DISABLED."
            )
            return

        # Set the authorization token
        slack.set_token(self.config["slack_token"])

        for rule in self.config["slack_rules"]:

            logger_name = rule["logger"]
            channel = rule["channel"]
            level = rule.get("level", "INFO").upper()

            log = logging.getLogger(logger_name)

            handler = slack.SlackLogHandler(channel)
            handler.setLevel(level)
            log.addHandler(handler)

    def _register_config(self):
        # Register config with comet broker
        try:
            enable_comet = self.config["comet_broker"]["enabled"]
        except KeyError as e:
            raise ConfigError("Missing config value 'comet_broker/enabled'.") from e
        if enable_comet:
            try:
                comet_host = self.config["comet_broker"]["host"]
                comet_port = self.config["comet_broker"]["port"]
            except KeyError as exc:
                raise InternalError(
                    "Failure registering initial config with comet broker: 'comet_broker/{}' "
                    "not defined in config.".format(exc)
                ) from exc
            comet = Manager(comet_host, comet_port)
            try:
                comet.register_start(datetime.datetime.utcnow(), __version__)
                comet.register_config(self.config)
            except CometError as exc:
                raise InternalError(
                    "Comet failed registering CoCo startup and initial config: {}".format(
                        exc
                    )
                ) from exc
        else:
            logger.warning("Config registration DISABLED. This is only OK for testing.")

    def _load_config(self, config_path: os.PathLike):

        self.config = config.load_config(config_path)

        self.log_level = self.config["log_level"]
        logger.setLevel(self.config["log_level"])
        # Also set log level for root logger, inherited by all
        logging.getLogger().setLevel(self.config["log_level"])

        # Get the state storage and blocklist path, if it's not absolute then it is resolved
        # relative to the config directory
        self.blocklist_path = Path(self.config["blocklist_path"])
        if not self.blocklist_path.is_absolute():
            raise ConfigError(
                f"Blocklist path \"{self.config['blocklist_path']}\" must be absolute."
            )
        storage_path = Path(self.config["storage_path"])
        if not storage_path.is_absolute():
            raise ConfigError(
                f"Storage path \"{self.config['storage_path']}\" must be absolute."
            )
        if not storage_path.is_dir():
            raise ConfigError(
                f"Storage path \"{self.config['storage_path']}\" doesn't exist."
            )

        # Read groups
        self.groups = self.config["groups"].copy()
        for group, hosts in self.groups.items():
            self.groups[group] = [Host(h) for h in hosts]

        # Init state, tries loading from persistent storage
        self.state = State(
            self.config["log_level"],
            storage_path,
            self.config["load_state"],
            self.config["exclude_from_reset"],
        )

        # Validate slack posting rules
        # TODO: move into config.py
        for rdict in self.config["slack_rules"]:
            if "logger" not in rdict or "channel" not in rdict:
                logger.error(f"Invalid slack rule {rdict}.")

    def _load_endpoints(self):

        self.endpoints = {}

        for conf in self.config["endpoints"]:

            name = conf["name"]

            # Create the endpoint object
            self.endpoints[name] = Endpoint(name, conf, self.forwarder, self.state)

            if self.endpoints[name].group not in self.groups:
                if not self.endpoints[name].has_external_forwards:
                    logger.debug(
                        f"Endpoint {name} has `call` set to 'null'. This means it "
                        f"doesn't call external endpoints. It might check other coco "
                        f"endpoints or return some part of coco's state."
                    )
                else:
                    raise RuntimeError(
                        f"Host group '{self.endpoints[name].group}' used by endpoint "
                        f"{name} unknown."
                    )
            self.forwarder.add_endpoint(name, self.endpoints[name])

    def _local_endpoints(self):
        # Register any local endpoints

        endpoints = {
            "blocklist": ("GET", self.forwarder.blocklist.process_get),
            "update-blocklist": ("POST", self.forwarder.blocklist.process_post),
            "saved-states": ("GET", self.state.get_saved_states),
            "reset-state": ("POST", self.state.reset_state),
            "save-state": ("POST", self.state.save_state),
            "load-state": ("POST", self.state.load_state),
            "wait": ("POST", wait.process_post),
        }

        for name, (type_, callable_) in endpoints.items():
            self.endpoints[name] = LocalEndpoint(name, type_, callable_)
            self.forwarder.add_endpoint(name, self.endpoints[name])

    def _check_endpoint_links(self):
        def check(e):
            if e:
                for a in e:
                    if isinstance(a, dict):
                        if len(a.keys()) != 1:
                            raise ConfigError(
                                f"coco.endpoint: bad config format for endpoint "
                                f"`{e.name}`: `{a}`. Should be either a string or "
                                f"have the format:\n```\nbefore:\n  - endpoint_name:\n   "
                                f"   identical: True\n```"
                            )
                        a = list(a.keys())[0]
                    if isinstance(a, CocoForward):
                        a = a.name
                    if a not in self.endpoints.keys():
                        raise ConfigError(
                            f"coco.endpoint: endpoint `{a}` found in config for "
                            f"`{e.name}` does not exist."
                        )

        for endpoint in self.endpoints.values():
            if hasattr(endpoint, "before"):
                check(endpoint.before)
            if hasattr(endpoint, "after"):
                check(endpoint.after)
            if hasattr(endpoint, "forward_to_coco"):
                check(endpoint.forward_to_coco)

    async def external_endpoint(self, request, endpoint):
        """
        Receive all HTTP calls.

        Core endpoint. Passes all endpoint calls on to redis and blocks until completion.
        """
        # create a unique name for this task: <process ID>-<POSIX timestamp>
        now = time.time()
        name = f"{os.getpid()}-{now}"

        with await self.redis_async as r:
            # Check if queue is full. If not, add this task.
            if self.config["queue_length"] > 0:
                full = await r.evalsha(
                    self.queue_sha,
                    keys=["queue", name],
                    args=[
                        self.config["queue_length"],
                        "method",
                        request.method,
                        "endpoint",
                        endpoint,
                        "request",
                        request.body,
                        "params",
                        request.query_string,
                        "received",
                        now,
                    ],
                )

                if full:
                    # Increment dropped request counter
                    await r.incr(f"dropped_counter_{endpoint}")
                    return response.json(
                        {"reply": "Coco queue is full.", "status": 503}, status=503
                    )
            else:
                # No limit on queue, just give the task to redis
                await r.hmset(
                    name,
                    "method",
                    request.method,
                    "endpoint",
                    endpoint,
                    "request",
                    request.body,
                    "params",
                    request.query_string,
                    "received",
                    now,
                )

                # Add task name to queue
                await r.rpush("queue", name)

            # Wait for the result (operations must be in this order to ensure
            # the result is available)
            code = int((await r.blpop(f"{name}:code"))[1])
            result = (await r.blpop(f"{name}:res"))[1]
            await r.delete(f"{name}:res")
            await r.delete(f"{name}:code")

        return response.raw(
            result, status=code, headers={"Content-Type": "application/json"}
        )
