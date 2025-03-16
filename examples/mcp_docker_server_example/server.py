import functools
import io
from typing import Any

import docker
from mcp.server.fastmcp import Context, FastMCP

from taskex import TaskRunner

client = docker.DockerClient(base_url="unix:///var/run/docker.sock")

mcp = FastMCP("Example")
runner = TaskRunner()


@mcp.tool()
async def cancel_command(
    ctx: Context,
    tracking_token: str,
) -> str:
    """Cancel a running command.

    Args:
        ctx: MCP context for providing progress updates
        tracking_token: The token to use in tracking the running command

    Returns:
        str: A message specifying successful cancellation or the error that occured during cancellation.

    """
    try:
        await runner.cancel(tracking_token)
        return f"Command {tracking_token} cancelled successfully"
    except Exception as e:
        return f"Failed to cancel command {tracking_token}: {e}"


@mcp.tool()
async def get_command_status(
    ctx: Context,
    tracking_token: str,
) -> str:
    """Get the status of an executed command.

    Args:
        ctx: MCP context for providing progress updates
        tracking_token: The token to use in tracking the running command

    Returns:
        str: The status string of the current task run output. If the command is still running, the function will return a status of "RUNNING".

    Examples:
        "Show me the output of my latest command"
        "What is the output of run test:1234567890?"
        "What is the status of the command with tracking token of test:1234567890?"
        "What is the status of build test:1234567890?"
    """

    ctx.info(f"Getting output of task {tracking_token}...")
    response = await runner.get_task_update(tracking_token)
    return response.status.value


@mcp.tool()
async def get_command_output(
    ctx: Context,
    tracking_token: str,
    as_json: bool = False,
) -> str:
    """Get the output of an executed command. If as_json is True, the output is
    returned as a JSON string. Otherwise, the output is returned as a string. The command
    that created the output must have been run with the `as_json` flag set to True. The run_id
    must be from a mcp tool call previously issued by the user.

    Args:
        ctx: MCP context for providing progress updates
        tracking_token: The token to use in tracking the running command
        as_json: Whether to return the output as a JSON string

    Returns:
        Text output or a JSON string if as_json is True.
        If the command is still running, the response will be "Command is still running".

    Examples:
        "Show me the output of my latest command"
        "Get the output of command test:1234567890 as JSON"
    """
    ctx.info(f"Getting output of task {tracking_token}...")
    response = await runner.get_task_update(tracking_token)

    if as_json and response.complete():
        return response.model_dump_json()

    if response.error and len(response.error) > 0:
        return response.error

    if response.complete():
        return response.result

    return "Command is still running."


@mcp.tool()
async def run_dcrx_script(
    ctx: Context,
    path: str,
    image: str,
    tag: str | None = None,
    dockerfile: str | None = None,
):
    args = [
        "build",
        path,
        image,
    ]

    if tag:
        args.extend(
            [
                "-t",
                tag,
            ]
        )

    if dockerfile:
        args.extend(
            [
                "-d",
                dockerfile,
            ]
        )

    run = runner.command(
        "dcrx",
        *args,
        alias="build_image",
    )

    return run.token


@mcp.tool()
async def build(
    ctx: Context,
    path: str,
    image_name: str,
    target: str | None = None,
    custom_context: str | None = None,
    platform: str | None = None,
    build_args: str | None = None,
    no_cache: bool = False,
    network_mode: str | None = None,
    timeout: str | int | float | None = None,
):
    """Build the Docker image at the specified path.

    Args:
        ctx: MCP context for providing progress updates
        path: The path to the Dockerfile to build
        image_name: The image_name specified as <name>:<tag> to be used in tracking.
        target: Optional target stage to pass to the Docker build
        custom_context: Optional path to a zipped tar or other artifact for Docker to use as a custom context in the build
        platform: Optional architecture to build the image for
        build_args: Optional comma-delimited list of NAME=VALUE build args to use in the Docker image build
        no_cache: Optionally disable caching for the build
        network_mode: Optional network settings to use for the image build
        timeout: Optional timeout. If the timeout is specified as, for example, two minutes is should be formatted as 2m, thirty seconds 30s, a hour 1h, etc.

    Returns:
        str: The tracking_token to be used in subsequent calls to get_command_output and task tracking or error. If an error is retuned, the command should abort.

    Examples:
        "build myapp:latest"
        "build the image at ./images/Dockerfile"
        "build the image"

    """

    image_build_args: dict[str, Any] | None = None
    if build_args:
        for arg in build_args.split(","):
            name, value = arg.split("=", maxsplit=1)
            image_build_args[name] = value

    use_custom_context = custom_context is not None
    context: io.StringIO | io.BytesIO | None = None
    if custom_context:
        run = runner.run(
            open,
            custom_context,
            "b",
            alias="load_custom_context",
        )

        context: io.BytesIO = await runner.wait(run.token)

    run = runner.run(
        functools.partial(
            client.images.build,
            dockerfile=path,
            fileobj=context,
            tag=image_name,
            target=target,
            custom_context=use_custom_context,
            nocache=no_cache,
            buildargs=image_build_args,
            platform=platform,
            network_mode=network_mode,
            quiet=True,
        ),
        alias="build_image",
        timeout=timeout,
    )

    return run.token


@mcp.tool()
async def push(
    ctx: Context,
    image_name: str,
    tag: str | None = None,
):
    """Push the specified Docker image.

    Args:
        ctx: MCP context for providing progress updates
        image_name: The image_name specified as <name>:<tag> to be used in tracking.
        tag: Optional tag for the pushed image.

    Returns:
        str: The tracking_token to be used in subsequent calls to get_command_output and task tracking or error. If an error is retuned, the command should abort.

    Examples:
        "push myapp:latest"
        "push the image"

    """

    run = runner.run(
        functools.partial(
            client.images.push,
            image_name,
            tag=tag,
        ),
        alias="push_image",
    )

    return run.token


@mcp.tool()
async def pull(
    ctx: Context,
    image_name: str,
    tag: str | None = None,
):
    """Pull the specified Docker image.

    Args:
        ctx: MCP context for providing progress updates
        image_name: The image_name specified as <name>:<tag> to be used in tracking.
        tag: Optional tag for the pulled image.

    Returns:
        str: The tracking_token to be used in subsequent calls to get_command_output and task tracking or error. If an error is retuned, the command should abort.

    Examples:
        "pull myapp:latest"
        "pull the latest python slim Docker image"

    """

    run = runner.run(
        functools.partial(
            client.images.pull,
            image_name,
            tag=tag,
        ),
        alias="pull_image",
    )

    return run.token
