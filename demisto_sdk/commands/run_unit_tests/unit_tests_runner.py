import os
import shutil
import sqlite3
import tempfile
import traceback
from pathlib import Path
from typing import List

import coverage
from junitparser import JUnitXml

import demisto_sdk.commands.common.docker_helper as docker_helper
from demisto_sdk.commands.common.content_constant_paths import CONTENT_PATH, PYTHONPATH
from demisto_sdk.commands.common.logger import logger
from demisto_sdk.commands.content_graph.objects.base_content import BaseContent
from demisto_sdk.commands.content_graph.objects.integration_script import (
    IntegrationScript,
)
from demisto_sdk.commands.coverage_analyze.helpers import coverage_files
from demisto_sdk.commands.lint.helpers import stream_docker_container_output

DOCKER_PYTHONPATH = [
    f"/content/{path.relative_to(CONTENT_PATH)}"
    for path in PYTHONPATH
    if path.is_relative_to(CONTENT_PATH)
]

DEFAULT_DOCKER_IMAGE = "demisto/python:1.3-alpine"

PYTEST_RUNNER = f"{(Path(__file__).parent / 'pytest_runner.sh')}"
POWERSHELL_RUNNER = f"{(Path(__file__).parent / 'pwsh_test_runner.sh')}"

NO_TESTS_COLLECTED = 5


def fix_coverage_report_path(coverage_file: Path) -> bool:
    """

    Args:
        coverage_file: The coverage file to to fix (absolute file).

    Returns:
        True if the file was fixed, False otherwise.

    Notes:
        the .coverage files contain all the files list with their absolute path.
        but our tests (pytest step) are running inside a docker container.
        so we have to change the path to the correct one.

    """
    try:
        logger.debug(f"Editing coverage report for {coverage_file}")
        with tempfile.NamedTemporaryFile() as temp_file:
            # we use a tempfile because the original file could be readonly, this way we assure we can edit it.
            shutil.copy(coverage_file, temp_file.name)
            with sqlite3.connect(temp_file.name) as sql_connection:
                cursor = sql_connection.cursor()
                files = cursor.execute("SELECT * FROM file").fetchall()
                for id_, file in files:
                    if not file.startswith("/content"):
                        # means that the .coverage file is already fixed
                        continue
                    file = Path(file).relative_to("/content")
                    if (
                        not (CONTENT_PATH / file).exists()
                        or file.parent.name
                        not in file.name  # For example, in `QRadar_v3` directory we only care for `QRadar_v3.py`
                    ):
                        logger.debug(f"Removing {file} from coverage report")
                        cursor.execute(
                            "DELETE FROM file WHERE id = ?", (id_,)
                        )  # delete the file from the coverage report, as it is not relevant.
                    else:
                        cursor.execute(
                            "UPDATE file SET path = ? WHERE id = ?",
                            (str(CONTENT_PATH / file), id_),
                        )
                sql_connection.commit()
                logger.debug("Done editing coverage report")
            coverage_file.unlink()
            shutil.copy(temp_file.name, coverage_file)
            return True
    except Exception:
        logger.warning(f"Broken .coverage file found: {file}, deleting it")
        file.unlink(missing_ok=True)
        return False


def merge_coverage_report():
    coverage_path = CONTENT_PATH / ".coverage"
    coverage_path.unlink(missing_ok=True)
    cov = coverage.Coverage(data_file=coverage_path)
    if not (files := coverage_files()):
        logger.warning("No coverage files found, skipping coverage report.")
        return
    fixed_files = [file for file in files if fix_coverage_report_path(Path(file))]
    cov.combine(fixed_files, keep=True)
    cov.xml_report(outfile=str(CONTENT_PATH / "coverage.xml"))
    logger.info(f"Coverage report saved to {CONTENT_PATH / 'coverage.xml'}")


def unit_test_runner(file_paths: List[Path], verbose: bool = False) -> int:
    docker_client = docker_helper.init_global_docker_client()

    exit_code = 0
    for filename in file_paths:
        integration_script = BaseContent.from_path(Path(filename))
        if not isinstance(integration_script, IntegrationScript):
            logger.warning(f"Skipping {filename} as it is not a content item.")
            continue

        if (test_data_dir := (integration_script.path.parent / "test_data")).exists():
            (test_data_dir / "__init__.py").touch()

        working_dir = (
            f"/content/{integration_script.path.parent.relative_to(CONTENT_PATH)}"
        )
        runner = (
            POWERSHELL_RUNNER
            if integration_script.type == "powershell"
            else PYTEST_RUNNER
        )
        shutil.copy(runner, integration_script.path.parent / "test_runner.sh")
        docker_images = [integration_script.docker_image or DEFAULT_DOCKER_IMAGE]
        if os.getenv("GITLAB_CI"):
            docker_images = [
                f"docker-io.art.code.pan.run/{docker_image}"
                for docker_image in docker_images
            ]
        logger.debug(f"{docker_images=}")
        for docker_image in docker_images:
            logger.info(f"Running test for {filename} using {docker_image=}")
            try:
                docker_client.images.pull(docker_image)
                shutil.copy(
                    CONTENT_PATH
                    / "Tests"
                    / "scripts"
                    / "dev_envs"
                    / "pytest"
                    / "conftest.py",
                    integration_script.path.parent / "conftest.py",
                )
                container = docker_client.containers.run(
                    image=docker_image,
                    environment={
                        "PYTHONPATH": ":".join(DOCKER_PYTHONPATH),
                        "REQUESTS_CA_BUNDLE": "/etc/ssl/certs/ca-certificates.crt",
                        "PYTHONDONTWRITEBYTECODE": "1",
                    },
                    volumes=[
                        f"{CONTENT_PATH}:/content",
                        "/etc/ssl/certs/ca-certificates.crt:/etc/ssl/certs/ca-certificates.crt",
                        "/etc/pip.conf:/etc/pip.conf",
                    ],
                    command="sh test_runner.sh",
                    user=f"{os.getuid()}:{os.getgid()}",
                    working_dir=working_dir,
                    detach=True,
                )
                logger.debug(f"Running test in container {container.id}")
                stream_docker_container_output(
                    container.logs(stream=True),
                    logger.info if verbose else logger.debug,
                )
                # wait for container to finish
                if status_code := container.wait()["StatusCode"]:
                    if status_code == NO_TESTS_COLLECTED:
                        logger.warning(
                            f"No test are collected for {integration_script.path} using {docker_image}."
                        )
                        continue
                    if not (
                        integration_script.path.parent / ".report_pytest.xml"
                    ).exists():
                        raise Exception(
                            f"No pytest report found in {integration_script.path.parent}. Logs: {container.logs()}"
                        )
                    test_failed = False
                    for suite in JUnitXml.fromfile(
                        integration_script.path.parent / ".report_pytest.xml"
                    ):
                        for case in suite:
                            if not case.is_passed:
                                logger.error(
                                    f"Test for {integration_script.object_id} failed in {case.name} with error {case.result[0].message}: {case.result[0].text}"
                                )
                                test_failed = True
                    if not test_failed:
                        logger.error(
                            f"Error running unit tests for {integration_script.path} using {docker_image=}. Container reports  {status_code=}, logs: {container.logs()}"
                        )
                    exit_code = 1
                else:
                    logger.info(f"All tests passed for {filename} in {docker_image}")
                container.remove(force=True)
            except Exception as e:
                logger.error(
                    f"Failed to run test for {filename} in {docker_image}: {e}"
                )
                traceback.print_exc()
                exit_code = 1
            finally:
                # remove pytest.ini no matter the results
                shutil.rmtree(
                    integration_script.path.parent / ".pytest.ini", ignore_errors=True
                )
    try:
        merge_coverage_report()
    except Exception as e:
        logger.warning(f"Failed to merge coverage report: {e}")
    return exit_code
