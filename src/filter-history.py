"""gitの特定ディレクトリ以下の履歴を別のリポジトリに切り出すスクリプト."""
import contextlib
import dataclasses
import logging
import os
import shlex
import shutil
import subprocess
import sys
from argparse import ArgumentParser
from logging import Formatter, StreamHandler
from logging.handlers import RotatingFileHandler
from pathlib import Path

_logger = logging.getLogger(__name__)


@dataclasses.dataclass
class _RunConfig:
    """スクリプト実行のための設定."""

    src_repository: str
    dst_repository: str
    target_dir: Path
    is_public: bool
    is_archive: bool

    git_user_name: str | None
    git_user_email: str | None

    clean: bool

    default_timeout_sec: int
    dry_run: bool
    verbose: int


class _ScriptError(Exception):
    # スクリプト実行のエラーを表す.

    pass


def _change_root_directory(
    clone_dir: Path,  # リポジトリをクローンしたディレクトリパス
    target_dir: Path,  # 履歴を抽出したディレクトリパス
    dry_run: bool,
    timeout_sec: int,
) -> None:
    with _working_directory(clone_dir.absolute()):
        # 抽出したフォルダからファイルを移動するが、ルートフォルダに同盟があると移動できない
        # そのため、一度renameして元のフォルダを削除してから、再度元の名称に変更する
        root_folders = set([f.name for f in Path().iterdir()])
        target_files = list([f for f in target_dir.iterdir()])

        skip_folders = list()  # ルートフォルダに同盟があるためスキップするファイルリスト
        for filepath in target_files:
            new_name = filepath.name
            if filepath.name in root_folders:
                new_name += "_test"
                skip_folders.append((new_name, filepath.name))
            _logger.info(f"move: {filepath} to {new_name}")
            _run_command(
                ["git", "mv", str(filepath), str(new_name)],
                dry_run=dry_run,
                timeout_sec=timeout_sec,
            )

        # ルートのフォルダは移行済みのはずなので削除
        for old_path in root_folders:
            if old_path == ".git":
                continue
            _logger.info(f"remove: {old_path}")
            shutil.rmtree(old_path)
        for old_filename, new_filename in skip_folders:
            _logger.info(f"move: {old_filename} to {new_filename}")
            _run_command(
                ["git", "mv", str(old_filename), str(new_filename)],
                dry_run=dry_run,
                timeout_sec=timeout_sec,
            )


def _check_tools(dry_run: bool, timeout_sec: int) -> bool:
    """実行に必要なツールが存在するか確認する.

    Returns
    -------
    bool
        Trueの場合はツールが利用できる.

    Notes
    -----
    - バージョン情報が表示できなければツールが存在しない.
    - git filter-repoは <https://github.com/newren/git-filter-repo> をPATHに追加することで利用できる.
    """

    try:
        _run_command(
            ["git", "--version"],
            dry_run=dry_run,
            timeout_sec=timeout_sec,
        )
        _run_command(
            ["git", "filter-repo", "--version"],
            dry_run=dry_run,
            timeout_sec=timeout_sec,
        )
        _run_command(
            ["gh", "--version"],
            dry_run=dry_run,
            timeout_sec=timeout_sec,
        )
    except _ScriptError as e:
        _logger.error(f"Could not find tool: {e}")
        return False

    return True


def _create_gh_repo_and_set_upstream(
    git_repository: Path,  # upstreamを設定するディレクトリ名
    repository_name: str,  # 作成するリポジトリ名: `owner/repository-name`
    remote_name: str,  # リモートリポジトリを設定する名称
    is_public: bool,  # Trueの場合は公開リポジトリとなる
    dry_run: bool,
    timeout_set: int,
) -> None:
    _run_command(
        [
            "gh",
            "repo",
            "create",
            repository_name,
            "--public" if is_public else "--private",
        ],
        dry_run=dry_run,
        timeout_sec=timeout_set,
    )
    with _working_directory(git_repository):
        _run_command(
            [
                "git",
                "remote",
                "add",
                remote_name,
                f"https://github.com/{repository_name}.git",
            ],
            dry_run=dry_run,
            timeout_sec=timeout_set,
        )


def _filter_history(
    src_repository: str,  # 履歴を持つ元のリポジトリ名: `owner/repository-name`
    target_dir: Path,  # 履歴を抽出するフォルダパス: `test/path` -> `repository-name/test/path`
    clone_dir: Path,  # リポジトリをクローンするディレクトリパス
    dry_run: bool,
    timeout_sec: int,
) -> None:
    _run_command(
        ["gh", "repo", "clone", src_repository, str(clone_dir.absolute())],
        dry_run=dry_run,
        timeout_sec=timeout_sec,
    )
    with _working_directory(clone_dir.absolute()):
        _run_command(
            # windowsの場合にバックスラッシュが適切に処理できないのでスラッシュに変更
            ["git", "filter-repo", "--path", str(target_dir).replace("\\", "/")],
            dry_run=dry_run,
            timeout_sec=timeout_sec,
        )


def _gh_repo_archive(
    repository_name: str,  # アーカイブするリポジトリ名: `owner/repository-name`
    dry_run: bool,
    timeout_sec: int,
) -> None:
    _run_command(
        ["gh", "repo", "archive", repository_name, "-y"],
        dry_run=dry_run,
        timeout_sec=timeout_sec,
    )


def _git_commit(
    git_directory: Path,  # コミットするディレクトリパス
    user_name: str | None,  # コミットするユーザー名
    user_email: str | None,  # コミットするユーザーのメールアドレス
    message: str,  # コミット時のメッセージ
    dry_run: bool,
    timeout_sec: int,
) -> None:
    with _working_directory(git_directory.absolute()):
        if user_name is not None:
            _run_command(
                ["git", "config", "--local", "user.name", user_name],
                dry_run=dry_run,
                timeout_sec=timeout_sec,
            )
        if user_email is not None:
            _run_command(
                ["git", "config", "--local", "user.email", user_email],
                dry_run=dry_run,
                timeout_sec=timeout_sec,
            )
        _run_command(
            ["git", "add", "."],
            dry_run=dry_run,
            timeout_sec=timeout_sec,
        )
        _run_command(
            ["git", "commit", "-m", message],
            dry_run=dry_run,
            timeout_sec=timeout_sec,
        )


def _git_push(
    git_directory: Path,  # push作業を行うgitリポジトリのディレクトリ
    remote_name: str,  # リモートリポジトリ
    branch_name: str,  # pushするブランチ名
    dry_run: bool,
    timeout_sec: int,
) -> None:
    with _working_directory(git_directory):
        _run_command(
            ["git", "push", remote_name, branch_name],
            dry_run=dry_run,
            timeout_sec=timeout_sec,
        )


def _git_rename(
    git_directory: Path,  # ファイル変更するgitディレクトリのパス
    names: dict[Path, Path],  # (元のファイルパス, 変更後のファイルパス)
    dry_run: bool,
    timeout_sec: int,
) -> bool:  # renameが発生した場合にTrueが返る
    is_rename = False
    with _working_directory(git_directory):
        for src, dst in names.items():
            if not src.exists():
                _logger.info(f"{src} does not exist. skip.")
                continue

            _run_command(
                ["git", "mv", str(src), str(dst)],
                dry_run=dry_run,
                timeout_sec=timeout_sec,
            )
            is_rename = True

    return is_rename


def _main() -> None:
    """スクリプトのエントリポイント."""
    # 実行時引数の読み込み
    config = _parse_args()

    # ログ設定
    loglevel = {
        0: logging.ERROR,
        1: logging.WARNING,
        2: logging.INFO,
        3: logging.DEBUG,
    }.get(config.verbose, logging.DEBUG)
    _setup_logger(filepath=None, loglevel=loglevel)
    _logger.info(config)

    raw_dir = Path("data/raw")
    repository_dir = raw_dir / config.src_repository.split("/")[1]
    rename_files = {
        # (変更前の名称, 変更後の名称)
        Path("index.md"): Path("README.md"),
    }

    # 必要なツールが利用できるか先に確認する
    is_tools = _check_tools(
        dry_run=config.dry_run, timeout_sec=config.default_timeout_sec
    )
    if not is_tools:
        raise ValueError("could not find tools.")

    # 抽出するリポジトリをローカルに作成してから必要なデータの取り出し
    _filter_history(
        src_repository=config.src_repository,
        target_dir=config.target_dir,
        clone_dir=repository_dir,
        dry_run=config.dry_run,
        timeout_sec=config.default_timeout_sec,
    )

    # 元のリポジトリはルートからディレクトリを複数階層作っているはずなのでリポジトリルートにファイルを移動
    _change_root_directory(
        clone_dir=repository_dir,
        target_dir=config.target_dir,
        dry_run=config.dry_run,
        timeout_sec=config.default_timeout_sec,
    )
    _git_commit(
        git_directory=repository_dir,
        user_name=config.git_user_name,
        user_email=config.git_user_email,
        message="chore: change root directory.",
        dry_run=config.dry_run,
        timeout_sec=config.default_timeout_sec,
    )
    # あらかじめ指定したファイルだけは名称変更
    is_rename = _git_rename(
        git_directory=repository_dir,
        names=rename_files,
        dry_run=config.dry_run,
        timeout_sec=config.default_timeout_sec,
    )
    if is_rename:
        _git_commit(
            git_directory=repository_dir,
            user_name=config.git_user_name,
            user_email=config.git_user_email,
            message="chore: change filenames.",
            dry_run=config.dry_run,
            timeout_sec=config.default_timeout_sec,
        )

    # 抽出した履歴を作成するためのリポジトリを作成しpushできるように設定
    _create_gh_repo_and_set_upstream(
        git_repository=repository_dir,
        repository_name=config.dst_repository,
        remote_name="upstream",
        is_public=config.is_public,
        dry_run=config.dry_run,
        timeout_set=config.default_timeout_sec,
    )
    _git_push(
        git_directory=repository_dir,
        remote_name="upstream",
        branch_name="master",
        dry_run=config.dry_run,
        timeout_sec=config.default_timeout_sec,
    )

    # push後の後始末
    if config.is_archive:
        _gh_repo_archive(
            repository_name=config.dst_repository,
            dry_run=config.dry_run,
            timeout_sec=config.default_timeout_sec,
        )
    if config.clean:
        shutil.rmtree(repository_dir)


def _parse_args() -> _RunConfig:
    """スクリプト実行のための引数を読み込む."""
    parser = ArgumentParser(description="gitの特定フォルダ以下の履歴を別のリポジトリに切り出す.")

    parser.add_argument(
        "src_repository", help="履歴を抽出する元となるリポジトリ名(`owner/repository-name`)"
    )
    parser.add_argument("target_dir", type=Path, help="履歴を抽出するフォルダパス")
    parser.add_argument(
        "dst_repository", help="抽出したリポジトリをpushするリポジトリ名(`owner/repository-name`)"
    )
    parser.add_argument(
        "-p", "--is-public", action="store_true", help="push先のリポジトリをプライベートとする."
    )
    parser.add_argument(
        "-a", "--is-archive", action="store_true", help="push先のリポジトリを最後にarchiveする."
    )

    parser.add_argument("--git-user-name", default=None, help="git commitに利用するユーザー名.")
    parser.add_argument(
        "--git-user-email", default=None, help="git commitに利用するメールアドレス."
    )

    parser.add_argument(
        "-c", "--clean", action="store_true", help="最後にローカルのクローンしたフォルダを削除する."
    )

    parser.add_argument(
        "-t", "--default-timeout-sec", default=30, help="各コマンドのタイムアウト待ち時間."
    )
    parser.add_argument(
        "-n", "--dry-run", action="store_true", help="コマンドを実行せず実行するコマンドを表示する."
    )
    parser.add_argument(
        "-v", "--verbose", action="count", default=0, help="詳細メッセージのレベルを設定."
    )

    args = parser.parse_args()
    config = _RunConfig(**vars(args))

    return config


def _run_command(
    command_args: list[str],  # 実行コマンドの配列: `["ls", "-la"]`
    dry_run: bool,  # Trueの場合はdry run実行
    timeout_sec: int,  # コマンドのタイムアウト待ち時間(sec)
) -> None:
    _logger.info(f"=== command: `{shlex.join(command_args)}`")
    if dry_run:
        # dry run実行のためコマンドだけ出力して何もしない
        return

    proc = subprocess.Popen(command_args, encoding="utf-8")
    try:
        proc.communicate(timeout=timeout_sec)
        if proc.returncode != 0:
            raise _ScriptError(f"command failed with exit status {proc.returncode}")
    except Exception:
        proc.kill()
        raise
    _logger.info("=== success command")


def _setup_logger(
    filepath: Path | None,  # ログ出力するファイルパス. Noneの場合はファイル出力しない.
    loglevel: int,  # 出力するログレベル
) -> None:
    # ログ出力設定
    # ファイル出力とコンソール出力を行うように設定する。
    _logger.setLevel(loglevel)

    # consoleログ
    console_handler = StreamHandler(stream=sys.stdout)
    console_handler.setLevel(loglevel)
    console_handler.setFormatter(
        Formatter("[%(levelname)7s] %(asctime)s (%(name)s) %(message)s")
    )
    _logger.addHandler(console_handler)

    # ファイル出力するログ
    # 基本的に大量に利用することを想定していないので、ログファイルは多くは残さない。
    if filepath is not None:
        file_handler = RotatingFileHandler(
            filepath,
            encoding="utf-8",
            mode="a",
            maxBytes=10 * 1024 * 1024,  # 10 MB
            backupCount=1,
        )
        file_handler.setLevel(loglevel)
        file_handler.setFormatter(
            Formatter("[%(levelname)7s] %(asctime)s (%(name)s) %(message)s")
        )
        _logger.addHandler(file_handler)


@contextlib.contextmanager
def _working_directory(path: Path):
    """Change working directory and returns to previous on exit."""
    prev_cwd = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(prev_cwd)


if __name__ == "__main__":
    try:
        _main()
    except Exception as e:
        _logger.exception(e)
        sys.exit(1)
