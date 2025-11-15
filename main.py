from core.agents.MuLi import MuLi
from rich.console import Console
from rich.markdown import Markdown
console = Console()

ml = MuLi(console=console)
while True:
    user_input = input("> ")
    response = ml.chat(user_input)
    console.print(Markdown(response))
