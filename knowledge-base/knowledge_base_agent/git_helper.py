import subprocess
import datetime
import logging
from pathlib import Path
from .exceptions import KnowledgeBaseError
from typing import List, Optional
import git
from knowledge_base_agent.config import Config
from knowledge_base_agent.exceptions import GitSyncError, CommandError
from git import Repo, GitCommandError
import asyncio
from functools import partial

def run_git_command(cmd: List[str], cwd: Path, capture_output: bool = False) -> Optional[str]:
    logging.debug(f"Running git command: {' '.join(cmd)}")
    try:
        result = subprocess.run(
            cmd,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=True
        )
        if capture_output:
            logging.debug(f"Command output: {result.stdout}")
            return result.stdout.strip()
        return None
    except subprocess.CalledProcessError as e:
        logging.error(f"Git command failed: {e.stderr}")
        raise KnowledgeBaseError(f"Git command failed: {e.stderr}")

class GitHelper:
    """Handles Git operations for the knowledge base."""
    
    def __init__(
        self,
        repo_path: Path,
        repo_url: str,
        token: str,
        user_name: str,
        user_email: str
    ):
        """
        Initialize GitHelper.
        
        Args:
            repo_path: Path to the repository
            repo_url: URL of the remote repository
            token: GitHub token
            user_name: Git user name
            user_email: Git user email
            
        Raises:
            GitError: If initialization fails
        """
        self.repo_path = repo_path
        self.repo_url = repo_url
        self.token = token
        self.user_name = user_name
        self.user_email = user_email
        self.repo: Optional[git.Repo] = None

    def initialize_repo(self) -> None:
        """Initialize or verify the git repository."""
        try:
            if not (self.repo_path / '.git').exists():
                self.repo = git.Repo.init(self.repo_path)
                self._setup_remote()
                # Set up initial branch and upstream
                self.repo.git.checkout('-B', 'main')  # Create and switch to main branch
                self.repo.git.push('--set-upstream', 'origin', 'main')  # Set upstream branch
            else:
                self.repo = git.Repo(self.repo_path)
                # Ensure remote exists
                if 'origin' not in [r.name for r in self.repo.remotes]:
                    self._setup_remote()
                    self.repo.git.push('--set-upstream', 'origin', 'main')
        except Exception as e:
            raise KnowledgeBaseError(f"Failed to initialize repository: {e}")

    def _setup_remote(self) -> None:
        """
        Set up the remote repository.
        
        Raises:
            GitError: If remote setup fails
        """
        try:
            remote_url = self.repo_url.replace('https://', f'https://{self.token}@')
            self.repo.create_remote('origin', remote_url)
        except Exception as e:
            raise KnowledgeBaseError(f"Failed to setup remote: {e}")

    def commit_and_push(self, commit_message: Optional[str] = None) -> None:
        """
        Commit changes and push to remote.
        
        Args:
            commit_message: Optional custom commit message
            
        Raises:
            GitError: If commit or push fails
        """
        try:
            if not self.repo:
                self.initialize_repo()

            # Configure user
            self.repo.config_writer().set_value("user", "name", self.user_name).release()
            self.repo.config_writer().set_value("user", "email", self.user_email).release()

            # Add all changes
            self.repo.git.add(all=True)

            # Check if there are changes to commit
            if self.repo.is_dirty(untracked_files=True):
                message = commit_message or f"Update knowledge base: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                self.repo.index.commit(message)
                
                # Push changes
                origin = self.repo.remote('origin')
                origin.push()
                
                logging.info("Successfully pushed changes to remote repository")
            else:
                logging.info("No changes to commit")
                
        except Exception as e:
            raise KnowledgeBaseError(f"Failed to commit and push changes: {e}")

    async def run_command(self, cmd: str, cwd: Path) -> None:
        """Run a git command asynchronously"""
        process = await asyncio.create_subprocess_exec(
            *cmd.split(),
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            raise CommandError(f"Command failed: {stderr.decode()}")

def push_to_github(
    knowledge_base_dir: Path,
    github_repo_url: str,
    github_token: str,
    git_user_name: str,
    git_user_email: str,
    commit_message: Optional[str] = None
) -> None:
    """
    Push changes to GitHub.
    
    Raises:
        GitError: If the operation fails
    """
    try:
        helper = GitHelper(
            repo_path=knowledge_base_dir,
            repo_url=github_repo_url,
            token=github_token,
            user_name=git_user_name,
            user_email=git_user_email
        )
        helper.commit_and_push(commit_message)
    except Exception as e:
        raise KnowledgeBaseError(f"Failed to push to GitHub: {e}")

class GitSyncHandler:
    """Handles Git operations for syncing knowledge base changes."""
    
    def __init__(self, config: Config):
        self.config = config
        self.repo = None
        
    async def _configure_git(self) -> None:
        """Configure git with user credentials."""
        if not self.repo:
            self.repo = Repo(str(self.config.knowledge_base_dir))
        
        with self.repo.config_writer() as git_config:
            git_config.set_value('user', 'name', self.config.github_user_name)
            git_config.set_value('user', 'email', self.config.github_user_email)

    async def sync_to_github(self, commit_message: str = "Update knowledge base content") -> None:
        try:
            await self._configure_git()
            repo = self.repo
            
            # Add and commit changes
            repo.git.add(A=True)
            if repo.is_dirty(untracked_files=True):
                repo.index.commit(commit_message)
                
                try:
                    # Try merge instead of rebase for README changes
                    logging.info("Pulling latest changes from remote...")
                    repo.git.pull('--no-rebase', 'origin', repo.active_branch.name)
                except GitCommandError as e:
                    if "CONFLICT" in str(e):
                        logging.warning("Resolving README conflict...")
                        # Keep our version of README.md
                        repo.git.checkout('--ours', 'README.md')
                        repo.git.add('README.md')
                        repo.git.commit('-m', 'Resolve README.md conflict')
                
                # Push changes
                try:
                    repo.remote('origin').push()
                except GitCommandError as e:
                    if "non-fast-forward" in str(e):
                        logging.warning("Non-fast-forward push, forcing update...")
                        repo.remote('origin').push(force_with_lease=True)
                
                logging.info("Successfully synced to GitHub")
            else:
                logging.info("No changes to commit")
                
        except Exception as e:
            logging.error(f"GitHub sync failed: {e}")
            raise GitSyncError(f"Git sync failed: {e}")

    async def handle_git_conflicts(self, repo_path: Path):
        try:
            # Try standard merge
            await self.run_command('git pull --rebase origin master', repo_path)
        except CommandError as e:
            if 'CONFLICT' in e.output:
                logging.warning("Resolving merge conflict by keeping both changes")
                await self.run_command('git checkout --ours README.md', repo_path)
                await self.run_command('git add README.md', repo_path)
                await self.run_command('git rebase --continue', repo_path)
