"""
A Spawner for JupyterHub that runs each user's server in a separate Docker Service
"""

from asyncio import sleep
from textwrap import dedent
from concurrent.futures import ThreadPoolExecutor
from pprint import pformat
import docker
from docker.errors import APIError, NotFound
from docker.tls import TLSConfig
from docker.types import (
    EndpointSpec,
    ServiceMode,
    Resources,
    RestartPolicy,
    UpdateConfig,
    RollbackConfig,
    Healthcheck,
    DNSConfig,
    Privileges,
    Mount,
    NetworkAttachmentConfig,
    SecretReference,
    ConfigReference,
    DriverConfig
)
from docker.utils import kwargs_from_env
from tornado import gen
from jupyterhub.spawner import Spawner
from traitlets import default, Dict, List, Unicode
from flatten_dict import flatten, unflatten

class SwarmSpawner(Spawner):
    """
    A Spawner for JupyterHub using Docker Engine in Swarm mode
    """

    default_config = Dict(
        default_value= 
        {
            "image": "jupyterhub/singleuser:latest"
        },
        config=True,
        help=dedent(
            """
            Default service configuration.

            Check for more info:
            https://docker-py.readthedocs.io/en/stable/services.html
            """
        ),
    )

    profiles = List(
        trait=Dict(),
        default_value=[],
        config=True,
        help=dedent(
            """
            List of Docker configuration profiles available for the user.

            Profile is the dict with name of the profile, title displayed in
            the html form and config: Docker service configuration.
            """
        ),
    )

    name_prefix = Unicode(
        "jupyter",
        config=True,
        help=dedent(
            """
            Prefix for service names.
            
            The full service name for a particular user will be
            <name_prefix>-<user_name>[-<server_name>].
            """
        ),
    )

    docker_client_tls_config = Dict(
        config=True,
        help=dedent(
            """
            Arguments to pass to docker TLS configuration.
            
            Check for more info:
            http://docker-py.readthedocs.io/en/stable/tls.html.
            """
        ),
    )

    form_template = Unicode(
        """
        <label for="profile">Select configuration:</label>
        <select class="form-control" name="profile" required autofocus>
            {option_template}
        </select>
        """,
        config=True,
        help="Form template.",
    )

    option_template = Unicode(
        """<option value="{name}" {selected}>{title}</option>""",
        config=True,
        help="Template for html form options.",
    )

    @default("options_form")
    def _options_form(self):
        if not self.profiles:
            return ""
        options = "".join([self.option_template.format(
            name=prof["name"],
            title=prof.get("title", prof["name"]),
            selected=("selected" if i == 0 else "")) for i, prof in enumerate(self.profiles)
            ])
        return form_template.format(option_template=options)

    def options_from_form(self, form_data):
        selected = form_data.get("profile")
        if selected:
            for prof in self.profiles:
                if prof["name"] == selected:
                    return prof
        return {}

    service_id = Unicode()

    service_name = Unicode()

    @default("service_name")
    def _service_name(self):
        return (self.format_string("{prefix}-{username}-{server}")
                if self.name else
                self.format_string("{prefix}-{username}"))

    _executor = None

    @property
    def executor(self, max_workers=1):
        """Single global executor"""
        cls = self.__class__
        if cls._executor is None:
            cls._executor = ThreadPoolExecutor(max_workers)
        return cls._executor

    _client = None

    @property
    def client(self):
        """Single global client instance"""
        cls = self.__class__

        if cls._client is None:
            kwargs = {}
            if self.docker_client_tls_config:
                kwargs["tls"] = TLSConfig(**self.docker_client_tls_config)
            kwargs.update(kwargs_from_env())
            client = docker.APIClient(version="auto", **kwargs)
            cls._client = client

        return cls._client

    def load_state(self, state):
        super().load_state(state)
        self.service_id = state.get("service_id", "")

    def get_state(self):
        state = super().get_state()
        if self.service_id:
            state["service_id"] = self.service_id
        return state

    def clear_state(self):
        super().clear_state()
        self.service_id = ""

    def _docker(self, method, *args, **kwargs):
        """
        Wrapper for calling docker methods to be passed to ThreadPoolExecutor
        """
        m = self.client
        for attr in method.split('.'):
            m = getattr(m, attr)
        return m(*args, **kwargs)

    def docker(self, method, *args, **kwargs):
        """
        Call a docker method in a background thread returns a Future
        """
        return self.executor.submit(self._docker, method, *args, **kwargs)

    @gen.coroutine
    def get_service(self):
        self.log.debug(
            "Getting Docker service {}".format(self.service_name)
        )
        try:
            service = yield self.docker("services.get", self.service_name)
            self.service_id = service.id
        except NotFound:
                self.log.info("Docker service {} is gone".format(self.service_name))
                service = None
                # Docker service is gone, remove service id
                self.service_id = ""
        except APIError as err:
            if err.is_server_error():
                self.log.info("Docker Swarm Server error")
                service = None
                # Docker service is unhealthy, remove the service_id
                self.service_id = ""
            else:
                raise
        return service

    def _format_mount(self, mount):
        if isinstance(mount, str):
            return self.format_string(mount)
        elif isinstance(mount, dict):
            mount["target"] = self.format_string(mount["target"])
            mount["source"] = self.format_string(mount["source"])
            return mount
    
    def get_service_config(self):
        config = {}

        config["name"] = self.service_name
        config["command"] = self.cmd
        config["args"] = self.get_args()
        config["env"] = ["{}={}".format(k, v) for k, v in self.get_env().items()]

        resources = {}
        if self.cpu_limit:
            resources["cpu_limit"] = self.cpu_limit * 10e9
        if self.mem_limit:
            mem = self.mem_limit
            resources["mem_limit"] = mem.lower() if isinstance(mem, str) else mem
        if self.cpu_guarantee:
            resources["cpu_reservation"] = self.cpu_guarantee * 10e9
        if self.mem_guarantee:
            mem = self.mem_guarantee
            resources["mem_reservation"] = mem.lower() if isinstance(mem, str) else mem
        if resources:
            config["resources"] = resources

        if self.default_config:
            config = _update_config(config, default_config)

        profile_name = ""
        if self.user_options:
            profile_name = self.user_options.get("name", "")
            profile_config = self.user_options.get("config", {})
            config = _update_config(config, profile_config)

        labels = {
            "org.jupyterhub.user": self.user.name,
            "org.jupyterhub.server": self.name,
            "org.jupyterhub.profile": profile_name
        }
        if "labels" in config:
            config["labels"].update(labels)
        else:
            config["labels"] = labels
    
        mounts = config.get("mounts")
        if mounts:
            config["mounts"] = [self._format_mount(mount) for mount in mounts]

        return config

    @gen.coroutine
    def start(self):
        """
        Start the single-user server in a docker service.
        You can specify the params for the service through
        jupyterhub_config.py or using the user_options.
        """
        service = yield self.get_service()

        if service is None:
            config = self.get_service_config()
            config = _parse_config(config)
            service = yield self.docker("services.create", **config)
            self.service_id = service.id
            self.log.info(
                "Created Docker service {} with id {} from image {} for user {}".format(
                    self.service_name, self.service_id[:7], config["image"], self.user.name
                )
            )
            yield self.wait_for_running_tasks()
        else:
            self.log.info(
                "Found existing Docker service {} with id {}".format(
                    self.service_name, self.service_id[:7]
                )
            )
            # Handle re-using API token.
            # Get the API token from the environment variables
            # of the running service:
            envs = service.attrs["Spec"]["TaskTemplate"]["ContainerSpec"]["Env"]
            for line in envs:
                if line.startswith("JPY_API_TOKEN="):
                    self.api_token = line.split("=", 1)[1]
                    break

        # We use service_name instead of ip
        # https://docs.docker.com/engine/swarm/networking/#use-swarm-mode-service-discovery
        # port should be default (8888)
        ip = self.service_name
        port = self.port

        return ip, port

    @gen.coroutine
    def stop(self, now=False):
        """
        Stop and remove the service
        Consider using stop/start when Docker adds support
        """
        self.log.info(
            "Stopping and removing Docker service {} with id {}".format(
                self.service_name, self.service_id[:7]
            )
        )

        try:
            result = yield self.docker("remove_service", self.service_name)
            # Even though it returns the service is gone
            # the underlying containers are still being removed
            if result:
                self.log.info(
                    "Docker service {} with id {} was removed".format(
                        self.service_name, self.service_id[:7]
                    )
                )
        except NotFound:
            self.log.warn("Docker service {} not found", self.server_name)
        except APIError:
            self.log.error("Error removing Docker service {} with id {}".format(
                self.server_name, self.service_id
            ))

        self.clear_state()

    @gen.coroutine
    def poll(self):
        """Check for a task state like `docker service ps id`"""

        tasks = yield docker("tasks", {"service": self.service_name})
        if not tasks:
            self.log.warn("Tasks for service {} not found", self.service_name)

        running_task = None
        for task in tasks:
            task_state = task["Status"]["State"]
            if task_state == "running":
                self.log.debug(
                    "Task {} of service with id {} status: {}".format(
                        task["ID"][:7], self.service_id[:7], pformat(task_state)
                    ),
                )
                # There should be at most one running task
                running_task = task
            if task_state == "rejected":
                task_err = task["Status"]["Err"]
                self.log.error(
                    "Task {} of service with id {} status: {} message: {}".format(
                        task["ID"][:7],
                        self.service_id[:7],
                        pformat(task_state),
                        pformat(task_err),
                    )
                )
                # If the tasks is rejected -> remove it
                yield self.stop()

        if running_task is not None:
            return None
        else:
            return 0

    @gen.coroutine
    def wait_for_running_tasks(self, max_attempts=20):
        preparing, running = False, False
        attempt = 0
        while not running:
            tasks = yield self.docker("tasks", {"service": self.service_name})
            preparing = False
            for task in tasks:
                task_state = task["Status"]["State"]
                self.log.info(
                    "Waiting for service {}, current task status: {}".format(
                        self.service_name, task_state
                    )
                )
                if task_state == "running":
                    running = True
                if task_state == "preparing":
                    preparing = True
                if task_state == "rejected" or attempt > max_attempts:
                    return False
            if not preparing:
                attempt += 1
            yield gen.sleep(1)

    def templete_namespace(self):
        profile = getattr(self, "user_options", {})
        return {
            "prefix": self.name_prefix,
            "username": self.user.name,
            "servername": self.name,
            "profile": profile.get("name", "")
        }

def _update_config(config, update):
    config = flatten(config)
    update = flatten(update)

    for opt, val1 in update.items():
        if isinstance(val1, list) and opt in config:
            val2 = config[opt]
            assert isinstance(val2, list)
            config[opt] = val1 + val2
        else:
            config[opt] = val1

    return unflatten(config)

_SERVICE_TYPES = {
    "endpoints": EndpointSpec,
    "mode": ServiceMode,
    "mounts": Mount,
    "networks": NetworkAttachmentConfig,
    "resources": Resources,
    "restart_policy": RestartPolicy,
    "secrets": SecretReference,
    "update_config": UpdateConfig,
    "roleback_config": RollbackConfig,
    "healthcheck": Healthcheck,
    "dns_config": DNSConfig,
    "configs": ConfigReference,
    "privileges": Privileges
}

_MOUNT_TYPES = {
    "driver_config": DriverConfig
}

def _parse_obj(obj, types):
    for opt, val in obj.items():
        opt_type = types.get(opt)
        if opt_type:
            if isinstance(val, dict):
                obj[opt] = opt_type(**val)
            elif isinstance(val, list):
                obj[opt] = [opt_type(**elm) if isinstance(elm, dict) else elm for elm in val]
    return obj

def _parse_config(self, config):
    mounts = config.get("mounts")
    if mounts:
        config["mounts"] = [_parse_obj(mount, _MOUNT_TYPES) for mount in mounts]
    config = _parse_obj(config, _SERVICE_TYPES)
    return config
