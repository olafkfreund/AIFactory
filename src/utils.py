"""Sample Python module."""
import os
from pathlib import Path

def hello():
    """Say hello."""
    print("Hello")

def goodbye():
    """Say goodbye."""
    print("Goodbye")

def new_function():
    """A new function."""
    return 42

class Greeter:
    """A greeter class."""

    def greet(self, name: str) -> str:
        return f"Hello, {name}"
