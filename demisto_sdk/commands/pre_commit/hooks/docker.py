import functools
import time
from collections import defaultdict
from copy import deepcopy
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

from docker.errors import DockerException

from demisto_sdk.commands.common.constants import TYPE_PWSH, TYPE_PYTHON
from demisto_sdk.commands.common.content_constant_paths import CONTENT_PATH, PYTHONPATH
from demisto_sdk.commands.common.docker_helper import (
    docker_login,
    get_docker,
    init_global_docker_client,
)
from demisto_sdk.commands.common.native_image import (
    NativeImageConfig,
    ScriptIntegrationSupportedNativeImages,
)
from demisto_sdk.commands.common.tools import logger
from demisto_sdk.commands.content_graph.objects.integration_script import (
    IntegrationScript,
)
from demisto_sdk.commands.lint.linter import DockerImageFlagOption
from demisto_sdk.commands.pre_commit.hooks.hook import Hook

NO_CONFIG_VALUE = None


@functools.lru_cache
def get_docker_python_path() -> str:
    """
    precommit by default mounts the content repo to source.
    This means CommonServerPython's path is /src/Packs/Base/...CSP.py
    Returns: A PYTHONPATH formatted string
    """
    path_to_replace = str(Path(CONTENT_PATH))
    docker_path = [str(path).replace(path_to_replace, "/src") for path in PYTHONPATH]
    path = ":".join(sorted(docker_path))
    logger.debug(f"pythonpath in docker being set to {path}")
    return path


def with_native_tags(
    tags_to_files: Dict[str, List[Tuple[Path, IntegrationScript]]],
    docker_image_flag: str,
) -> Dict[str, List[Tuple[Path, IntegrationScript]]]:
    """
    Adds the native image images into the dict with the files that should be run on them
    Args:
        tags_to_files: Dict[str, Tuple[str, dict] the incoming dict without native image of files split according to the docker images
        docker_image_flag: the flag from the config file. all/native:ga/native:maintenance etc

    Returns: The updated dict with the native images.

    """
    docker_flags = set(docker_image_flag.split(","))
    all_tags_to_files: Dict[str, List[Tuple[Path, IntegrationScript]]] = defaultdict(
        list
    )
    native_image_config = NativeImageConfig.get_instance()

    for image, scripts in tags_to_files.items():
        for file, obj in scripts:

            supported_native_images = ScriptIntegrationSupportedNativeImages(
                _id=obj.object_id,
                native_image_config=native_image_config,
                docker_image=image,
            ).get_supported_native_docker_tags(docker_flags)
            for native_image in supported_native_images:
                all_tags_to_files[native_image].append((file, obj))
            if {
                DockerImageFlagOption.FROM_YML.value,
                DockerImageFlagOption.ALL_IMAGES.value,
            } & docker_flags:
                all_tags_to_files[image].append((file, obj))
    return all_tags_to_files


def docker_tag_to_runfiles(
    files_to_run: Iterable[Tuple[Path, Optional[IntegrationScript]]], docker_image_flag
) -> Dict[str, List[Tuple[Path, IntegrationScript]]]:
    """
    Iterates over all files snf groups the files by the dockerimages
    Args:
        files_to_run: PosixFiles to run the command on
        docker_image_flag: the docker_image config value

    Returns: A dict of image to List of files(Tuple[path, obj]) including native images

    """
    tags_to_files: Dict[str, List[Tuple[Path, IntegrationScript]]] = defaultdict(list)
    for file, obj in files_to_run:
        if not obj:
            continue
        for docker_image in obj.docker_images:
            tags_to_files[docker_image].append((file, obj))
    return with_native_tags(tags_to_files, docker_image_flag)


@functools.lru_cache(maxsize=512)
def devtest_image(image_tag, is_powershell) -> str:
    """
    We need to add test dependencies on the image. In the future we could add "additional_dependencies" as a template
    config arg and pass it through here
    Args:
        image_tag: the base image tag
        is_powershell: if the image is a powershell based image

    Returns: The build and pulled dev image

    """
    all_errors: list = []
    for _ in range(2):  # retry it once
        logger.info(f"getting devimage for {image_tag}, {is_powershell=}")
        image, errors = get_docker().pull_or_create_test_image(
            base_image=image_tag,
            container_type=TYPE_PWSH if is_powershell else TYPE_PYTHON,
            push=docker_login(docker_client=init_global_docker_client()),
            log_prompt="DockerHook",
        )
        if not errors:
            return image
        all_errors.append(errors)
    raise DockerException(all_errors)


def get_environment_flag() -> str:
    """
    The env flag needed to run python scripts in docker
    """
    return f'--env "PYTHONPATH={get_docker_python_path()}"'


def _split_by_config_file(files, config_arg: Optional[Tuple]):
    """
    Will group files into groups that share the same configuration file.
    If there is no config file, they get set to the NO_CONFIG_VALUE group
    Args:
        files: the files to split
        config_arg: a tuple, argument_name, file_name

    Returns:
        a dict where the keys are the names of the folder of the config and the value is a set of files for that config
    """
    if not config_arg:
        return {NO_CONFIG_VALUE: files}
    folder_to_files = defaultdict(set)

    for file in files:
        if (file.parent / config_arg[1]).exists():
            folder_to_files[str(file.parent)].add(file)
        else:
            folder_to_files[NO_CONFIG_VALUE].add(file)  # type:ignore

    return folder_to_files


class DockerHook(Hook):
    """
    This class will make common manipulations on commands that need to run in docker
    """

    def prepare_hook(
        self,
        files_to_run_with_objects: Iterable[Tuple[Path, Optional[IntegrationScript]]],
        run_docker_hooks,
    ):
        """
        Group all the files by dockerimages
        Split those images by config files
        Get the devimage for each image
        Args:
            files_to_run: all files to run on
            run_docker_hooks: bool - Whether to run docker based hooks or skip them.

        """
        if not run_docker_hooks:
            return
        start_time = time.time()
        filtered_files = self.filter_files_matching_hook_config(
            (file for file, _ in files_to_run_with_objects)
        )
        filtered_files_with_objects = {
            (file, obj)
            for file, obj in files_to_run_with_objects
            if file in filtered_files
        }
        tag_to_files_objs = docker_tag_to_runfiles(
            filtered_files_with_objects,
            self._get_property("docker_image", "from-yml"),
        )
        end_time = time.time()
        logger.debug(
            f"Elapsed time to gather tags to files: {end_time - start_time} seconds"
        )
        config_arg = self._get_config_file_arg()

        start_time = time.time()
        logger.info(f"{len(tag_to_files_objs)} images were collected from files")
        logger.debug(f'collected images: {" ".join(tag_to_files_objs.keys())}')
        for image, files_with_objects in sorted(
            tag_to_files_objs.items(), key=lambda item: item[0]
        ):

            paths = {file for file, obj in files_with_objects}
            folder_to_files = _split_by_config_file(paths, config_arg)
            image_is_powershell = any(
                obj.is_powershell for _, obj in files_with_objects
            )

            dev_image = devtest_image(  # consider moving to before loop and threading.
                image, image_is_powershell
            )
            hooks = self.get_new_hooks(dev_image, image, folder_to_files, config_arg)
            self.hooks.extend(hooks)

        end_time = time.time()
        logger.info(
            f"DockerHook - Elapsed time to prep all the images: {end_time - start_time} seconds"
        )

    def get_new_hooks(
        self,
        dev_image,
        image,
        folder_to_files: Dict[str, Set[Path]],
        config_arg: Optional[Tuple],
    ):
        """
        Given the docker image and files to run on it, create new hooks to insert
        Args:
            dev_image: The actual image to run on
            image: name of the base image (for naming)
            folder_to_files: A dict where the key is the folder and value is the set of files to run together.
            config_arg: The config arg to set where relevant. This will be appended to the end of "args"
        Returns:
            All the hooks to be appended for this image
        """
        new_hook = deepcopy(self.base_hook)
        new_hook["id"] = f"{new_hook.get('id')}-{image}"
        new_hook["name"] = f"{new_hook.get('name')}-{image}"
        new_hook["language"] = "docker_image"
        new_hook[
            "entry"
        ] = f'--entrypoint {new_hook.get("entry")} {get_environment_flag()} {dev_image}'

        ret_hooks = []
        counter = 0
        for folder, files in folder_to_files.items():
            hook = deepcopy(new_hook)
            if config_arg and folder is not NO_CONFIG_VALUE:
                args = deepcopy(self._get_property("args", []))
                args.extend(
                    [
                        config_arg[0],  # type:ignore
                        str(list(files)[0].parent / config_arg[1]),  # type:ignore
                    ]  # type:ignore
                )  # type:ignore
                hook["args"] = args
                hook["id"] = f"{hook['id']}-{counter}"  # for uniqueness
                hook["name"] = f"{hook['name']}-{counter}"
                counter += 1
            if self._set_files_on_hook(hook, files):
                ret_hooks.append(hook)
        for hook in ret_hooks:
            if hook.get("docker_image"):
                hook.pop("docker_image")
            if hook.get("config_file_arg"):
                hook.pop("config_file_arg")
        return ret_hooks

    def _get_config_file_arg(self) -> Optional[Tuple]:
        """
        A config arg should be of the format
            config_file_arg:
                arg_name: '--argname'
                file_name: '.filename'
        Returns: argname, filename

        """
        if config_arg := self._get_property("config_file_arg"):
            arg_name = config_arg.get("arg_name")
            file_name = config_arg.get("file_name")
            if not arg_name or not file_name:
                raise ValueError(
                    f"config_file_arg was provided in pre-commit hook with id {self._get_property('id')}"
                    "  in configuration file but not properly formed. Must have arg_name and file_name keys"
                )
            return arg_name, file_name
        return None
