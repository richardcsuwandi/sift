"""Sift command-line interface and conversational terminal session."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

import typer
from prompt_toolkit import PromptSession
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.history import FileHistory
from platformdirs import user_state_path
from rich.console import Console

from app import __version__
from app.config import OLLAMA_MODEL
from app.plan import load_plan, save_plan
from app.service import SiftService
from app.terminal import (
    BLUE,
    banner,
    consume_events,
    consume_organize_events,
    print_json,
    render_answer,
    render_plan,
    review_plan,
    models_panel,
    status_panel,
)


cli = typer.Typer(
    name="sift",
    help="Your local AI file copilot.",
    no_args_is_help=False,
    rich_markup_mode="rich",
    pretty_exceptions_show_locals=False,
)
console = Console()
err_console = Console(stderr=True)


def _service() -> SiftService:
    try:
        return SiftService()
    except Exception as exc:
        err_console.print(f"[red]Sift could not start:[/] {exc}")
        raise typer.Exit(1)


def _root(value: Optional[Path]) -> Path:
    try:
        return SiftService.root(value)
    except ValueError as exc:
        err_console.print(f"[red]Error:[/] {exc}")
        raise typer.Exit(2)


def _make_plan(service, root, events, instruction, kind, model):
    suggestions, error = (
        consume_organize_events(console, events, root=root)
        if kind == "organize"
        else consume_events(console, events, label="Sifting…")
    )
    if error:
        err_console.print(f"[red]{error}[/]")
        raise typer.Exit(1)
    return service.build_plan(
        root, suggestions, instruction=instruction, kind=kind, model=model
    )


def _apply(service: SiftService, value: dict) -> dict:
    result = service.apply_plan(value)
    if result["applied"]:
        console.print(
            f"[green]Applied {result['applied']} change{'s' if result['applied'] != 1 else ''}.[/] "
            f"[dim]Batch {result['batch_id'][:8]} · /undo to restore[/]"
        )
    else:
        console.print("[yellow]No changes were applied.[/]")
    for item in result.get("stale") or []:
        console.print(f"[yellow]Skipped[/] {item['path']}: {item['skip_reason']}")
    return result


@cli.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    model: str = typer.Option(OLLAMA_MODEL, "--model", "-m", help="Ollama chat model."),
    no_color: bool = typer.Option(False, "--no-color", help="Disable terminal colors."),
    version: bool = typer.Option(False, "--version", is_eager=True, help="Show the installed Sift version."),
) -> None:
    """Open Sift's interactive terminal when no subcommand is supplied."""
    if version:
        console.print(f"Sift {__version__}")
        raise typer.Exit()
    if ctx.invoked_subcommand is not None:
        return
    if no_color:
        console.no_color = True
    interactive(None, model=model)


@cli.command()
def scan(
    folder: Optional[Path] = typer.Argument(None, help="Folder to index; defaults to the current folder."),
    images: bool = typer.Option(False, "--images", help="Read image contents with a vision model."),
    vision_model: Optional[str] = typer.Option(None, help="Ollama vision model."),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    service, root = _service(), _root(folder)
    last = {}
    events = service.scan_events(root, read_images=images, vision_model=vision_model)
    if json_output:
        for event in events:
            last = event
            print(json.dumps(event, ensure_ascii=False))
    else:
        _, error = consume_events(console, events, label="Scanning…")
        if error:
            raise typer.Exit(1)
        last = service.status(root)
        console.print(f"[green]Indexed {last['file_count']} files[/] in {root}")


@cli.command()
def ask(
    question: str = typer.Argument(..., help="Question to ask about indexed files."),
    folder: Optional[Path] = typer.Option(None, "--folder", "-C"),
    model: str = typer.Option(OLLAMA_MODEL, "--model", "-m"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    service = _service()
    if folder is not None:
        _root(folder)
    root = _root(folder)
    try:
        result = service.ask(question, model=model, root=root)
    except (ValueError, OSError) as exc:
        err_console.print(f"[red]Error:[/] {exc}")
        raise typer.Exit(1)
    print_json(console, result) if json_output else render_answer(console, result)


@cli.command("plan")
def plan_command(
    instruction: str = typer.Argument(..., help="Natural-language file action."),
    folder: Optional[Path] = typer.Option(None, "--folder", "-C"),
    model: str = typer.Option(OLLAMA_MODEL, "--model", "-m"),
    save: Optional[Path] = typer.Option(None, "--save", help="Write the plan to JSON."),
    apply: bool = typer.Option(False, "--apply", help="Review and apply the plan now."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Apply without an interactive confirmation."),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    service, root = _service(), _root(folder)
    value = _make_plan(
        service, root, service.command_events(root, instruction, model=model),
        instruction, "command", model,
    )
    if save:
        save_plan(value, save)
    if json_output:
        print_json(console, value)
    elif not apply:
        render_plan(console, value)
        if save:
            console.print(f"[green]Saved[/] {save.expanduser().resolve()}")
        else:
            console.print("[dim]Nothing changed. Add --save or --apply.[/]")
    if apply:
        approved = yes or review_plan(console, value, typer.prompt, default_save=save)
        if approved:
            _apply(service, value)


@cli.command()
def organize(
    folder: Optional[Path] = typer.Argument(None, help="Folder to organize."),
    model: str = typer.Option(OLLAMA_MODEL, "--model", "-m"),
    save: Optional[Path] = typer.Option(None, "--save"),
    apply: bool = typer.Option(False, "--apply"),
    yes: bool = typer.Option(False, "--yes", "-y"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    service, root = _service(), _root(folder)
    value = _make_plan(
        service, root, service.organize_events(root, model=model),
        "Organize this folder", "organize", model,
    )
    if save:
        save_plan(value, save)
    if json_output:
        print_json(console, value)
    elif not apply:
        render_plan(console, value)
        console.print("[dim]Nothing changed. Add --save or --apply.[/]")
    if apply and (yes or review_plan(console, value, typer.prompt, default_save=save)):
        _apply(service, value)


@cli.command("apply")
def apply_saved(
    plan_file: Path = typer.Argument(..., exists=True, dir_okay=False),
    yes: bool = typer.Option(False, "--yes", "-y"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    service = _service()
    try:
        value = load_plan(plan_file)
        _root(Path(value["root"]))
    except ValueError as exc:
        err_console.print(f"[red]Invalid plan:[/] {exc}")
        raise typer.Exit(2)
    approved = yes or review_plan(console, value, typer.prompt, default_save=plan_file)
    if not approved:
        console.print("[dim]No changes applied.[/]")
        return
    result = _apply(service, value)
    if json_output:
        print_json(console, result)


@cli.command()
def undo(
    batch: Optional[str] = typer.Option(None, "--batch"),
    yes: bool = typer.Option(False, "--yes", "-y"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    if not yes and not typer.confirm("Undo the selected batch?", default=False):
        return
    reverted = _service().undo(batch)
    result = {"reverted": reverted, "batch_id": batch}
    if json_output:
        print_json(console, result)
    else:
        console.print(
            f"[green]Restored {reverted} item{'s' if reverted != 1 else ''}.[/]"
            if reverted else "[yellow]Nothing to undo.[/]"
        )


@cli.command()
def status(
    folder: Optional[Path] = typer.Argument(None),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    value = _service().status(_root(folder))
    print_json(console, value) if json_output else status_panel(console, value)


@cli.command()
def models(json_output: bool = typer.Option(False, "--json")) -> None:
    value = _service().models()
    if json_output:
        print_json(console, value)
        return
    models_panel(console, value)
    if not value["installed"]:
        console.print("[yellow]No models found. Start Ollama and pull qwen3:4b.[/]")


@cli.command()
def reveal(path: Path = typer.Argument(..., exists=True, dir_okay=False)) -> None:
    """Select a file in Finder (or the platform file manager)."""
    try:
        target = _service().reveal(path)
    except (ValueError, OSError) as exc:
        err_console.print(f"[red]Error:[/] {exc}")
        raise typer.Exit(1)
    console.print(f"[green]Revealed[/] {target}")


HELP = """[bold]Conversation[/]
  Ask where a file is, or describe a move, rename, trash, or organization task.

[bold]Commands[/]
  /scan [--images]  rescan files; add --images to read image contents
  /organize         propose a complete folder structure
  /plan             show the pending changes
  /apply            review and apply the pending plan
  /undo             restore the last applied batch
  /folder PATH      switch the active folder
  /model [NAME]     list or select an Ollama model
  /reveal           select the top search result in Finder
  /status           show session configuration
  /help             show this guide
  /clear            clear the screen
  /exit             leave Sift

[dim]Natural-language changes are always planned and reviewed before they touch disk.[/]"""


def _is_action(text: str) -> bool:
    lowered = text.casefold()
    return any(word in lowered for word in ("move ", "rename ", "trash ", "delete ", "put ", "group "))


def _is_organize(text: str) -> bool:
    lowered = text.casefold()
    return any(phrase in lowered for phrase in ("organize", "clean up", "tidy", "sort this", "sort the folder"))


def _refresh_index(
    service: SiftService, root: Path, *, read_images: bool = False
) -> bool:
    """Refresh the active index and leave the conversation usable on failure."""
    try:
        _, error = consume_events(
            console,
            service.scan_events(root, read_images=read_images),
            label="Reading files…" if read_images else "Preparing index…",
        )
    except KeyboardInterrupt:
        console.print("[yellow]Indexing stopped.[/] Partial progress was kept; chat is still available.")
        return False
    if error:
        console.print(f"[yellow]Index refresh failed:[/] {error}")
        return False
    count = service.status(root)["file_count"]
    console.print(
        f"[green]Ready.[/] {count} file{'s' if count != 1 else ''} indexed"
        + (" · image contents included" if read_images else "")
    )
    return True


def _reveal_top(service: SiftService, root: Path, results: list[dict]) -> Path | None:
    """Reveal the result the last answer ranked first."""
    if not results:
        console.print("[yellow]Ask a question first; there is no result to reveal.[/]")
        return None
    target = service.reveal(results[0]["path"], root=root)
    console.print(f"[green]Revealed[/] {target}")
    return target


def _review_conversational_plan(service: SiftService, plan: dict, prompt) -> dict | None:
    """Review immediately; return the plan only when the user postpones it."""
    if review_plan(console, plan, prompt):
        _apply(service, plan)
        return None
    console.print("[dim]Nothing changed. The plan is kept; use /plan or /apply later.[/]")
    return plan


def interactive(folder: Optional[Path], *, model: str) -> None:
    service = _service()
    root = _root(folder)
    commands = [
        "/scan", "/organize", "/plan", "/apply", "/undo", "/folder",
        "/model", "/reveal", "/status", "/help", "/clear", "/exit",
    ]
    state_dir = user_state_path("Sift", appauthor=False)
    state_dir.mkdir(parents=True, exist_ok=True)
    history_path = state_dir / "history"
    session = PromptSession(
        history=FileHistory(str(history_path)),
        completer=WordCompleter(commands, sentence=True),
        complete_while_typing=False,
    )
    pending = None
    last_results: list[dict] = []
    banner(console, service.status(root))
    _refresh_index(service, root)
    console.print("[dim]Describe what you need, or type /help. Changes always stop for review.[/]\n")

    def prompt(message="› ", default=""):
        return session.prompt(message, default=default)

    while True:
        try:
            text = prompt().strip()
        except KeyboardInterrupt:
            console.print("[dim]Cancelled. Press Ctrl-D or type /exit to leave.[/]")
            continue
        except EOFError:
            console.print("\n[dim]Goodbye.[/]")
            return
        if not text:
            continue
        command, _, argument = text.partition(" ")
        command = command.lower()
        try:
            if command in {"/exit", "/quit"}:
                console.print("[dim]Goodbye.[/]")
                return
            if command == "/help":
                console.print(HELP)
            elif command == "/clear":
                console.clear()
                banner(console, service.status(root))
            elif command == "/status":
                status_panel(console, service.status(root))
            elif command == "/folder":
                if not argument:
                    console.print(f"[dim]{root}[/]")
                else:
                    root = service.root(argument)
                    pending = None
                    last_results = []
                    banner(console, service.status(root))
                    _refresh_index(service, root)
            elif command == "/model":
                value = service.models()
                if argument:
                    candidate = argument.strip()
                    if candidate not in (value.get("chat_models") or []):
                        raise ValueError(
                            f"{candidate!r} is not an installed chat model. "
                            "Run /model to see model roles."
                        )
                    model = candidate
                    console.print(f"[green]Model set to[/] {model}")
                else:
                    models_panel(console, value, active=model)
            elif command == "/reveal":
                _reveal_top(service, root, last_results)
            elif command == "/scan":
                images = "--images" in argument.split()
                _refresh_index(service, root, read_images=images)
            elif command == "/organize" or (not text.startswith("/") and _is_organize(text)):
                instruction = text if not text.startswith("/") else "Organize this folder"
                suggestions, error = consume_organize_events(
                    console, service.organize_events(root, model=model), root=root
                )
                if error:
                    console.print(f"[red]{error}[/]")
                else:
                    pending = service.build_plan(
                        root, suggestions, instruction=instruction, kind="organize", model=model
                    )
                    pending = _review_conversational_plan(service, pending, prompt)
            elif command == "/plan":
                if pending:
                    render_plan(console, pending)
                else:
                    console.print("[yellow]There is no pending plan.[/]")
            elif command == "/apply":
                if not pending:
                    console.print("[yellow]There is no pending plan.[/]")
                elif review_plan(console, pending, prompt):
                    _apply(service, pending)
                    pending = None
            elif command == "/undo":
                if prompt("Undo the last batch? [y/N]: ").lower() in {"y", "yes"}:
                    reverted = service.undo()
                    console.print(f"[green]Restored {reverted} item{'s' if reverted != 1 else ''}.[/]")
            elif text.startswith("/"):
                console.print(f"[yellow]Unknown command {command}. Type /help.[/]")
            elif _is_action(text):
                suggestions, error = consume_events(
                    console, service.command_events(root, text, model=model), label="Planning…"
                )
                if error:
                    console.print(f"[red]{error}[/]")
                else:
                    pending = service.build_plan(
                        root, suggestions, instruction=text, kind="command", model=model
                    )
                    pending = _review_conversational_plan(service, pending, prompt)
            else:
                answer = service.ask(text, model=model, root=root)
                last_results = answer.get("results") or []
                render_answer(console, answer)
        except KeyboardInterrupt:
            console.print("\n[yellow]Stopped.[/] Partial index progress, if any, was kept.")
        except ValueError as exc:
            console.print(f"[red]Error:[/] {exc}")
        except Exception as exc:
            console.print(f"[red]Sift hit an unexpected error:[/] {exc}")


def run() -> None:
    # Click groups cannot unambiguously combine a positional folder on the
    # root command with named subcommands. Dispatch an existing path before
    # handing the rest to Typer, which keeps both `sift ~/Downloads` and
    # `sift scan ~/Downloads` natural.
    known = {"scan", "ask", "plan", "organize", "apply", "undo", "status", "models", "reveal"}
    args = sys.argv[1:]
    if args and not args[0].startswith("-") and args[0] not in known:
        folder = Path(args[0]).expanduser()
        if folder.is_dir():
            model = OLLAMA_MODEL
            if "--model" in args:
                position = args.index("--model")
                if position + 1 < len(args):
                    model = args[position + 1]
            elif "-m" in args:
                position = args.index("-m")
                if position + 1 < len(args):
                    model = args[position + 1]
            interactive(folder, model=model)
            return
    cli()


if __name__ == "__main__":
    run()
