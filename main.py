from rich.console import Console
from rich.markdown import Markdown

console = Console()

console.print("[green]正在加载工具，请稍候...[/green]")
from core.agents.MuLi import MuLi
ml = MuLi(console=console)
console.print("[green]加载完成！[/green]")

while True:
    user_input = input("> ")
    response = ml.chat(user_input)
    console.print(Markdown(response))
