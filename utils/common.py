import os

from utils.settings import settings


def get_workspace() -> str:
    workspace = settings.workspace_dir
    
    if not workspace:
        return os.getcwd()
    
    return os.path.abspath(workspace)


if __name__ == "__main__":
    print(get_workspace())
