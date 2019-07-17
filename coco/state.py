"""coco state module."""
import hashlib
import logging
from typing import List
import orjson as json
import yaml

logger = logging.getLogger(__name__)


class State:
    """Representation of the complete state of all hosts (configs) coco controls."""

    def __init__(self, log_level, state=dict()):
        """
        Construct the state.

        Parameters
        ----------
        log_level : str
            Log level to use inside this class.
        state : dict
            If not `None` the state is initialized with this. Default `None`.
        """
        self._state = state
        logger.setLevel(log_level)

    def write(self, path, value, name=None):
        """
        Write (or overwrite) a value in the state.

        Parameters
        ----------
        path : str
            `"path/to/write/value/to"`. If `name` is `None`, the last part of the path will be the
            name of the entry.
        value
            The value.
        name : str
            The name of the entry. If this is `None` the last part of `path` will be used.
        """
        if name is None:
            element, name = self._find_new(path)
        else:
            element = self._find(path)
        element[name] = value

    def read(self, path, name=None):
        """
        Read a value from the state.

        Parameters
        ----------
        path : str
            `"path/to/the/value"`. If `name` is `None`, the last part of this is the name of the
            value to read.
        name : str
            Name of the value. If this is `None`, the last part of `path` will be used.

        Returns
        -------
        The value.
        """
        element = self._find(path)
        if name:
            return element[name]
        return element

    def extract(self, path: str) -> dict:
        """
        Extract a part of the state containing the whole given path.

        Parameters
        ----------
        path : str
            `"path/to/the/value"`. The last part of this is the name of the value to read.

        Returns
        -------
        dict
            A dict that contains the root level of the state and the whole requested path, but only
            the values in the requested entry.
        """
        value = self.read(path)
        parts = path.split("/")

        def pack(p: List[str], v) -> dict:
            """
            Pack a value into a nested dict.

            Parameters
            ----------
            p : list
                Path for nested dict.
            v
                Value.

            Returns
            -------
            dict
                A nested dict containing the full given path and only the one given value at the
                bottom.
            """
            if len(p) == 0:
                return v
            if len(p) == 1:
                return dict({p[0]: value})
            return dict({p[0]: pack(p[1:], v)})

        return pack(parts, value)

    def read_from_file(self, path, file):
        """
        Write into the state from what is read from a file.

        Parameters
        ----------
        path : str
            `"path/to/the/new/state/entry"`
        file : str
            Name of the file to read from.
        """
        logger.debug(f"Loading state {path} from file {file}.")
        element, name = self._find_new(path)

        with open(file, "r") as stream:
            try:
                element[name] = yaml.safe_load(stream)
            except yaml.YAMLError as exc:
                logger.error(f"Failure reading YAML file {file}: {exc}")

    def _find(self, path):
        """
        Find `"an/entry/by/path"` and return the entry.

        Parameters
        ----------
        path : str
            `"path/to/the/entry"`

        Returns
        -------
            The state entry.
        """
        if path is None or path == "" or path == "/":
            return self._state
        paths = path.split("/")
        element = self._state
        for i in range(0, len(paths)):
            element = element[paths[i]]
        return element

    def _find_new(self, path):
        """
        Find `"an/entry/by/path/and/name"` and return the parent entry and `name` of the new entry.

        Parameters
        ----------
        path : str
            `"path/to/the/entry"`

        Returns
        -------
            The parent entry and the name of the new entry (can be used like
            `parent_entry[name] = <new_value>`).
        """
        if path is None or path == "" or path == "/":
            raise RuntimeError("Can't create new state entry at root level.")
        paths = path.split("/")
        element = self._state
        for i in range(0, len(paths) - 1):
            element = element[paths[i]]
        return element, paths[-1]

    def find_or_create(self, path):
        """
        Find or create `"a/path/in/the/state"`.

        Parameters
        ----------
        path : str
            `"a/path/in/the/state"`.

        Returns
        -------
        dict
            The part of the state the path points at.
        """
        if path is None:
            return None
        if path is None or path == "" or path == "/":
            return self._state
        paths = path.split("/")
        element = self._state
        for i in range(0, len(paths)):
            try:
                element = element[paths[i]]
            except TypeError:
                raise RuntimeError(
                    f"coco.state: part {i} of path {path} is of type "
                    f"{type(element).__name__}. Can't overwrite it with a sub-"
                    f"state block."
                )
            except KeyError:
                element[paths[i]] = dict()
                element = element[paths[i]]

        return element

    def hash(self, path=None):
        """
        Calculate the hash of any part of the state. or of the whole state if `path` is `None`.

        Parameters
        ----------
        path : str
            `"path/to/entry"`. Default `None`.

        Returns
        -------
            The hash for the selected part of the state.
        """
        element = self._find(path)
        serialized = json.dumps(element, sort_keys=True, separators=(",", ":"))

        _md5 = hashlib.md5()
        _md5.update(serialized)
        return _md5.hexdigest()
