from mcp.server.fastmcp import FastMCP, Context
from taskex import TaskRunner
from dcrx import Image

mcp = FastMCP("Example")
runner = TaskRunner()
images: dict[str, Image] = {}


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
    
    return 'Command is still running.'

@mcp.tool()
async def build_image(
    ctx: Context,
    name: str,
    tag: str | None = None
):
    """Run a common linux shell command as you would in your terminal.

    Args:
        ctx: MCP context for providing progress updates
        name: The name of the image.
        tag: An optional image tag.

    Returns:
        str: The image's full name specified as <name>:<tag>.

    Examples:
        "List all current files in my home directory"
        "Grep the file test.txt for any numbers"
        "Find all files matching the name script.py"
        "Build the docker image at image/Dockerfile and tag it as "
    
    """

    image_full_name = f'{name}:{tag}'

    if tag:
        images[image_full_name] = Image(
            name,
            tag=tag
        )

    else:
        images[image_full_name] = Image(name)

    return image_full_name

    