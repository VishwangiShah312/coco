"""
Endpoint farm for testing coco.

Simulates multiple hosts with endpoints.
"""

from flask import Flask, request, jsonify
import threading
import requests
import socket
from contextlib import closing

app = Flask(__name__)
counters = dict()
threads = list()
callbacks = dict()


@app.route("/<name>")
def endpoint(name):
    """Accept any endpoint call."""
    print(f"{name} called, received {request.json}")
    try:
        counters[int(request.host.split(":")[1])][name] += 1
    except KeyError:
        counters[int(request.host.split(":")[1])][name] = 1
    try:
        return jsonify(callbacks[name](request.json))
    except KeyError:
        return None


def shutdown_server():
    """Stop a flask server."""
    func = request.environ.get("werkzeug.server.shutdown")
    if func is None:
        raise RuntimeError("Not running with the Werkzeug Server")
    func()


@app.route("/shutdown", methods=["POST"])
def shutdown():
    """Receive calls to /shutdown and stop server."""
    shutdown_server()
    return f"Shutting down test endpoints on {request.host}..."


def find_free_port():
    """Return an unused port."""
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("", 0))
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return s.getsockname()[1]


def flask_thread(port):
    """Run a flask web server."""
    app.run(port=port, debug=True, use_reloader=False)


class Farm:
    """
    Endpoint farm.

    Run many flask webservers accepting endpoint calls and counting them for test purposes.
    Count all endpoint calls.
    """

    def __init__(self, ports, callbacks):
        self.ports = self.start_farm(ports, callbacks)

    def __del__(self):
        """
        Destructor.

        Stop the farm.
        """
        self.stop_farm()

    @staticmethod
    def start_farm(n_ports, _callbacks):
        """
        Start the farm.

        Parameters
        ----------
        n_ports : int
            Number of webservers to start with different ports.
        _callbacks : dict
            Names of the endpoints and functions they should call.

        Returns
        -------
        list
            The ports the new Flask servers are assigned to.
        """
        global threads
        global callbacks
        global counters

        callbacks = _callbacks
        ports = list()

        for i in range(n_ports):
            port = find_free_port()
            counters[port] = dict()
            ports.append(port)
            print("Started test endpoints on port {}.".format(port))
            t = threading.Thread(target=flask_thread, args=(port,))
            t.daemon = True
            t.start()
            threads.append(t)
        return ports

    def stop_farm(self):
        """Stop the farm."""
        for port in self.ports:
            reply = requests.post("http://localhost:" + str(port) + "/shutdown")
        print(reply.text)

    @staticmethod
    def counters():
        """Return endpoint call counters."""
        global counters
        return counters

    @property
    def hosts(self):
        """Return a list of host names (e.g. "http://localhost:1234/")."""
        hosts = list()
        for port in self.ports:
            hosts.append("http://localhost:" + str(port) + "/")
        return hosts