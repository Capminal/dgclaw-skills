"""Load strategy config from JSON file."""
import json
import os


def load_strategy(strategy_path=None):
    """Load strategy config. Resolves 'current' pointer if no path given."""
    strategies_dir = os.path.join(os.path.dirname(__file__), '..')

    if strategy_path is None:
        current_file = os.path.join(strategies_dir, 'current')
        with open(current_file) as f:
            strategy_name = f.read().strip()
        strategy_path = os.path.join(strategies_dir, strategy_name)

    with open(strategy_path) as f:
        return json.load(f)
