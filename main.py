#!/usr/bin/env python3
"""
git_automation_cli_main.py

A Git Automation CLI tool that:
- Scans a parent directory for project subfolders (unlimited)
- For each project folder:
  - Initializes git if needed
  - Configures user.name and user.email
  - Creates a GitHub repository (using user's PAT) named after the folder
  - Adds remote origin (SSH or HTTPS)
  - Makes initial commit and pushes (to a chosen default branch)
  - Optionally performs extra branch operations per user flags

Requirements:
    pip install PyGithub colorama tqdm

Security note:
- Each user must provide their own GitHub Personal Access Token (PAT).
- Avoid hard-coding PATs into files. Use environment variables or prompt input.

Usage example:
    python git_automation_cli_main.py \
        --parent /home/user/projects \
        --token-from-env GITHUB_TOKEN \
        --name "Your Name" --email "you@example.com" \
        --protocol HTTPS --private --default-branch main

"""

from __future__ import annotations
import argparse
import os
import sys
import subprocess
import shutil
from pathlib import Path
from typing import List, Dict, Optional, Tuple
import getpass
import logging
import textwrap
import time
import logging


# Optional niceties
try:
    from github import Github, GithubException
except Exception:  # pragma: no cover - PyGithub not installed
    Github = None
    GithubException = Exception

try:
    from colorama import init as colorama_init, Fore, Style
    colorama_init(autoreset=True)
except Exception:  # pragma: no cover - colorama not installed
    class Dummy:
        def __getattr__(self, name):
            return ""
    Fore = Style = Dummy()

try:
    from tqdm import tqdm
except Exception:
    def tqdm(x, **kwargs):
        return x

# ---------- Helpers ----------

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("git-automation")


def run_git_cmd(args: List[str], cwd: Path, check: bool = True, capture_output: bool = False, env: Optional[Dict[str,str]] = None) -> subprocess.CompletedProcess:
    """Run a git (or other) command in a given working directory.
    Raises subprocess.CalledProcessError when check=True and the command fails.
    """
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    try:
        result = subprocess.run(args, cwd=str(cwd), check=check, text=True, stdout=(subprocess.PIPE if capture_output else None), stderr=(subprocess.PIPE if capture_output else None), env=full_env)
        return result
    except subprocess.CalledProcessError as e:
        if capture_output:
            logger.debug(f"Command {args} failed. stdout: {e.stdout} stderr: {e.stderr}")
        raise


def get_subdirectories(parent: Path) -> List[Path]:
    """Return a list of immediate subdirectories under parent, sorted by name in filesystem order.
    Hidden directories (starting with .) are ignored by default.
    """
    if not parent.is_dir():
        raise NotADirectoryError(f"Parent path is not a directory: {parent}")
    subdirs = [p for p in sorted(parent.iterdir()) if p.is_dir() and not p.name.startswith('.')]
    return subdirs


def default_initial_branch_name(repo_name: str) -> str:
    # Could be enhanced to read user's Git default branch preference.
    return "main"


# ---------- GitHub operations ----------

class GitHubManager:
    def __init__(self, token: str):
        if Github is None:
            raise RuntimeError("PyGithub is required. Install with: pip install PyGithub")
        self._g = Github(token)
        try:
            self._user = self._g.get_user()
            self.username = self._user.login
        except GithubException as e:
            raise RuntimeError(f"Failed to authenticate to GitHub: {e}")

    def create_repo(self, name: str, private: bool = False, description: str = "", auto_init: bool = False, default_branch: Optional[str] = None) -> 'github.Repository.Repository':
        """Create a repository under the authenticated user's account.
        If the repo already exists, we return the existing repo object.
        """
        try:
            # if repo exists, get it
            repo = None
            try:
                repo = self._user.get_repo(name)
                logger.info(Fore.YELLOW + f"Repo already exists on GitHub: {name}")
                return repo
            except Exception:
                pass

            kwargs = dict(name=name, private=private, description=description, auto_init=auto_init)
            if default_branch:
                # PyGithub doesn't accept default_branch in create_repo for older versions; set later if needed
                repo = self._user.create_repo(**kwargs)
                if default_branch and default_branch != repo.default_branch:
                    try:
                        repo.edit(name=name, default_branch=default_branch)
                    except Exception:
                        logger.debug("Could not change default branch via API; will use chosen branch when pushing.")
            else:
                repo = self._user.create_repo(**kwargs)
            logger.info(Fore.GREEN + f"Created GitHub repo: {name}")
            return repo
        except GithubException as e:
            raise RuntimeError(f"GitHub create repo failed: {e}")


# ---------- Core processing ----------

class ProjectProcessor:
    def __init__(self,
                 parent_dir: Path,
                 github_manager: Optional[GitHubManager],
                 token: Optional[str],
                 git_name: Optional[str],
                 git_email: Optional[str],
                 protocol: str = 'HTTPS',
                 private: bool = False,
                 default_branch: str = 'main',
                 push_via_token_in_https: bool = False,
                 do_branch_ops: Optional[Dict] = None):
        self.parent_dir = parent_dir
        self.github_manager = github_manager
        self.token = token
        self.git_name = git_name
        self.git_email = git_email
        self.protocol = protocol.upper()
        self.private = private
        self.default_branch = default_branch
        self.push_via_token_in_https = push_via_token_in_https
        self.do_branch_ops = do_branch_ops or {}

        # Summary
        self.summary: List[Tuple[str, bool, str]] = []  # (project, success, message)

    def process_all(self):
        projects = get_subdirectories(self.parent_dir)
        if not projects:
            logger.info(Fore.YELLOW + "No subdirectories found under the parent path.")
            return

        logger.info(Fore.CYAN + f"Found {len(projects)} projects. Beginning processing in order.")

        for project in tqdm(projects, desc="Projects"):
            try:
                self.process_project(project)
            except Exception as e:
                logger.error(Fore.RED + f"Failed processing {project.name}: {e}")
                self.summary.append((project.name, False, str(e)))

        # final report
        self.report_summary()

    def process_project(self, project_path: Path):
        logger.info(Style.BRIGHT + f"\n=== Processing: {project_path.name} ===")
        cwd = project_path

        # 1) check if .git exists
        git_dir = cwd / '.git'
        if not git_dir.exists():
            logger.info(Fore.YELLOW + ".git not found -> initializing git repository")
            run_git_cmd(["git", "init"], cwd)
        else:
            logger.info(Fore.GREEN + ".git found -> skipping git init")

        # 2) set git config for this repo (local config)
        if self.git_name:
            run_git_cmd(["git", "config", "user.name", self.git_name], cwd)
        if self.git_email:
            run_git_cmd(["git", "config", "user.email", self.git_email], cwd)

        # 3) prepare default branch name
        branch_name = self.default_branch or default_initial_branch_name(project_path.name)

        # Create an initial commit if repository has no commits
        has_commits = self._has_commits(cwd)
        if not has_commits:
            # ensure there's something to commit; if directory empty, create a README
            if not any(cwd.iterdir()):
                (cwd / 'README.md').write_text(f"# {project_path.name}\n\nInitial commit by git-automation CLI\n")
            # add all files
            try:
                run_git_cmd(["git", "add", "-A"], cwd)
                # ensure branch exists and switch to it
                run_git_cmd(["git", "checkout", "-b", branch_name], cwd)
                run_git_cmd(["git", "commit", "-m", "Initial commit"], cwd)
            except subprocess.CalledProcessError:
                # If commit fails (e.g., nothing to commit) continue
                logger.warning(Fore.YELLOW + "Initial commit failed or nothing to commit; continuing")
        else:
            # switch to chosen branch (create if missing)
            try:
                run_git_cmd(["git", "checkout", branch_name], cwd)
            except subprocess.CalledProcessError:
                run_git_cmd(["git", "checkout", "-b", branch_name], cwd)

        # 4) create GitHub repo (if manager passed)
        remote_url = None
        if self.github_manager:
            repo = self.github_manager.create_repo(project_path.name, private=self.private, default_branch=branch_name)
            if self.protocol == 'SSH':
                remote_url = repo.ssh_url
            else:
                remote_url = repo.clone_url  # https URL

            # If using HTTPS with token for non-interactive push, embed token into URL
            if self.protocol == 'HTTPS' and self.push_via_token_in_https and self.token:
                # repo.clone_url typically: https://github.com/user/repo.git
                # embed token: https://<token>@github.com/user/repo.git
                remote_url = remote_url.replace("https://", f"https://{self.token}@")

            # add remote (replace if exists)
            try:
                # if origin exists, set-url
                run_git_cmd(["git", "remote", "remove", "origin"], cwd, check=False)
            except Exception:
                pass
            run_git_cmd(["git", "remote", "add", "origin", remote_url], cwd)
            logger.info(Fore.GREEN + f"Remote origin set to: {remote_url}")

        # 5) push
        try:
            env = None
            if self.protocol == 'HTTPS' and not self.push_via_token_in_https:
                # attempt interactive HTTPS push; to reduce hanging we disable prompts
                env = {"GIT_TERMINAL_PROMPT": "0"}
            run_git_cmd(["git", "push", "-u", "origin", branch_name], cwd, env=env)
            logger.info(Fore.GREEN + f"Pushed {project_path.name} to origin/{branch_name}")
        except subprocess.CalledProcessError as e:
            # Push failed
            raise RuntimeError(f"Failed to push {project_path.name}: {e}")

        # 6) optional branch operations
        self._do_branch_operations(cwd)

        # append success
        self.summary.append((project_path.name, True, "OK"))

        # 7) go back to parent automatically (we've used cwd parameter; nothing to cd)
        logger.info(Style.BRIGHT + f"=== Finished: {project_path.name} ===\n")

    def _has_commits(self, cwd: Path) -> bool:
        try:
            run_git_cmd(["git", "rev-parse", "--is-inside-work-tree"], cwd, capture_output=True)
            # check log
            run_git_cmd(["git", "rev-parse", "HEAD"], cwd, check=True, capture_output=True)
            return True
        except subprocess.CalledProcessError:
            return False

    def _do_branch_operations(self, cwd: Path):
        # example: create branch
        if not self.do_branch_ops:
            return
        # create branch
        if 'create' in self.do_branch_ops and self.do_branch_ops['create']:
            name = self.do_branch_ops['create']
            try:
                run_git_cmd(["git", "checkout", "-b", name], cwd)
                run_git_cmd(["git", "push", "-u", "origin", name], cwd)
                logger.info(Fore.GREEN + f"Created and pushed branch {name}")
            except subprocess.CalledProcessError:
                logger.warning(Fore.YELLOW + f"Could not create branch {name}")
        # rename branch
        if 'rename' in self.do_branch_ops and self.do_branch_ops['rename']:
            old, new = self.do_branch_ops['rename']
            try:
                run_git_cmd(["git", "branch", "-m", old, new], cwd)
                run_git_cmd(["git", "push", "origin", ":" + old], cwd)
                run_git_cmd(["git", "push", "-u", "origin", new], cwd)
                logger.info(Fore.GREEN + f"Renamed branch {old} -> {new}")
            except subprocess.CalledProcessError:
                logger.warning(Fore.YELLOW + f"Could not rename branch {old} -> {new}")
        # switch branch
        if 'switch' in self.do_branch_ops and self.do_branch_ops['switch']:
            name = self.do_branch_ops['switch']
            try:
                run_git_cmd(["git", "checkout", name], cwd)
                logger.info(Fore.GREEN + f"Switched to branch {name}")
            except subprocess.CalledProcessError:
                logger.warning(Fore.YELLOW + f"Could not switch to branch {name}")

    def report_summary(self):
        ok = [s for s in self.summary if s[1]]
        fail = [s for s in self.summary if not s[1]]
        logger.info(Style.BRIGHT + "\n=== Summary ===")
        logger.info(Fore.GREEN + f"Succeeded: {len(ok)}")
        for name, _, msg in ok:
            logger.info(Fore.GREEN + f"  - {name}: {msg}")
        logger.info(Fore.RED + f"Failed: {len(fail)}")
        for name, _, msg in fail:
            logger.info(Fore.RED + f"  - {name}: {msg}")


# ---------- CLI ----------
logging.basicConfig(
    filename='git_automation.log',
    filemode='a',  # اضافه کردن به فایل (حذف نشدن لاگ‌های قبلی)
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

def parse_args():
    p = argparse.ArgumentParser(description="Git Automation CLI: batch-create GitHub repos and push project folders.")
    p.add_argument('--parent', '-P', required=True, help='Parent directory containing project subfolders')

    token_group = p.add_mutually_exclusive_group(required=False)
    token_group.add_argument('--token', help='GitHub Personal Access Token (not recommended to pass on CLI)')
    token_group.add_argument('--token-from-env', help='Name of environment variable that holds the GitHub token (recommended)')

    p.add_argument('--name', help='Git user.name to set locally for each repo', required=False)
    p.add_argument('--email', help='Git user.email to set locally for each repo', required=False)
    p.add_argument('--protocol', choices=['HTTPS', 'SSH'], default='HTTPS', help='Protocol to use for remotes')
    p.add_argument('--private', action='store_true', help='Create GitHub repos as private')
    p.add_argument('--default-branch', default='main', help='Default branch name to use for initial commits')

    p.add_argument('--embed-token-in-https', action='store_true', dest='https_token', help='Embed token into HTTPS remote URL for non-interactive push (insecure but sometimes necessary)')

    # branch operations (optional advanced)
    p.add_argument('--create-branch', help='Create and push a branch with this name after initial push')
    p.add_argument('--rename-branch', nargs=2, metavar=('OLD', 'NEW'), help='Rename a branch locally and remotely')
    p.add_argument('--switch-branch', help='Switch to an existing branch after push')

    p.add_argument('--yes', '-y', action='store_true', help='Assume yes for interactive prompts')
    return p.parse_args()


def main():
    args = parse_args()
    parent = Path(args.parent).expanduser().resolve()
    if not parent.is_dir():
        logger.error(Fore.RED + f"Parent path is not a directory: {parent}")
        sys.exit(1)

    # Obtain token
    token = None
    if args.token:
        token = args.token
    elif args.token_from_env:
        token = os.environ.get(args.token_from_env)
        if not token:
            logger.error(Fore.RED + f"Environment variable {args.token_from_env} not set or empty")
            sys.exit(1)
    else:
        # prompt securely
        if not args.yes:
            token = getpass.getpass(prompt='Enter your GitHub Personal Access Token (input hidden): ')
        else:
            logger.error(Fore.RED + "No token supplied and --yes set; cannot safely prompt for token.")
            sys.exit(1)

    if not token:
        logger.error(Fore.RED + "A GitHub token is required to create repositories via the API.")
        sys.exit(1)

    # Make GitHub manager
    try:
        gh = GitHubManager(token)
        logger.info(Fore.CYAN + f"Authenticated to GitHub as: {gh.username}")
    except Exception as e:
        logger.error(Fore.RED + f"GitHub authentication failed: {e}")
        sys.exit(1)

    # Prepare branch ops
    branch_ops = {}
    if args.create_branch:
        branch_ops['create'] = args.create_branch
    if args.rename_branch:
        branch_ops['rename'] = tuple(args.rename_branch)
    if args.switch_branch:
        branch_ops['switch'] = args.switch_branch

    processor = ProjectProcessor(
        parent_dir=parent,
        github_manager=gh,
        token=token,
        git_name=args.name,
        git_email=args.email,
        protocol=args.protocol,
        private=args.private,
        default_branch=args.default_branch,
        push_via_token_in_https=bool(args.https_token),
        do_branch_ops=branch_ops,
    )

    # Confirmation
    logger.info(Style.BRIGHT + f"Parent directory: {parent}")
    logger.info(f"Number of projects: will scan all immediate subdirectories")
    logger.info(f"GitHub account: {gh.username}")
    logger.info(f"Protocol: {args.protocol}")
    logger.info(f"Default branch: {args.default_branch}")
    logger.info(f"Create repos as private: {args.private}")
    if not args.yes:
        cont = input(Fore.YELLOW + "Proceed? [y/N]: ")
        if cont.lower() not in ('y', 'yes'):
            logger.info("Aborted by user")
            sys.exit(0)

    # Run
    start = time.time()
    processor.process_all()
    logger.info(Style.BRIGHT + f"All done in {time.time() - start:.1f}s")


if __name__ == '__main__':
    main()
