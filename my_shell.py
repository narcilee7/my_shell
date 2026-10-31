#!/usr/bin/env python3
"""
迷你 shell —— 学习用，~150 行，覆盖真实 shell 的四个核心机制：
  REPL 主循环 / 内建命令 / 重定向 / 管道

每个系统调用都是真实 shell（bash/zsh）在用的同一个：
  os.fork    → fork(2)      复制进程
  os.execvp  → execvp(2)    替换进程映像（带 PATH 查找）
  os.dup2    → dup2(2)      复制文件描述符（重定向/管道的关键）
  os.pipe    → pipe(2)      匿名管道
  os.waitpid → waitpid(2)   回收子进程，拿退出码

用法：python3 迷你shell.py
支持：ls -l | grep py > out.txt、cd、exit、$VAR、$?、Ctrl-C 杀前台不杀 shell
"""
import os
import sys
import shlex
import signal

LAST_STATUS = 0  # $? 的值


# ---------- v2: 内建命令 ----------
# 为什么 cd 必须是内建？当前工作目录是进程自己的属性，
# 外部程序 fork 出的子进程改了目录就退出，shell 自己纹丝不动。
# 所以"改变 shell 自身状态"的命令只能由 shell 亲自执行。

def bi_cd(args):
    path = args[0] if args else os.path.expanduser("~")
    try:
        os.chdir(path)
    except OSError as e:
        print(f"cd: {path}: {e.strerror}", file=sys.stderr)
        return 1
    return 0


def bi_exit(args):
    sys.exit(int(args[0]) if args else LAST_STATUS)


BUILTINS = {"cd": bi_cd, "exit": bi_exit}


# ---------- 解析：一行 → 命令链 + 重定向 ----------
# shlex 负责分词和引号；punctuation_chars 让 | > >> < 成为独立 token。

def parse(line):
    lex = shlex.shlex(line, posix=True, punctuation_chars="|<>")
    lex.whitespace_split = True
    tokens = list(lex)

    commands = []   # [["ls", "-l"], ["grep", "py"]]
    argv = []
    redirs = []     # (mode, filename)，mode ∈ {">", ">>", "<"}
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t == "|":
            commands.append(argv)
            argv = []
        elif t in (">", ">>", "<"):
            i += 1
            if i >= len(tokens):
                raise SyntaxError(f"{t} 后面缺文件名")
            redirs.append((t, tokens[i]))
        else:
            argv.append(expand(t))
        i += 1
    commands.append(argv)
    return commands, redirs


def expand(word):
    """$? 展开成上一条命令的退出码，$VAR 走环境变量。"""
    word = word.replace("$?", str(LAST_STATUS))
    return os.path.expandvars(word)


# ---------- v3/v4: 执行 ----------

def child_exec(argv, redirs):
    """子进程收尾：应用重定向，然后 exec。exec 成功就不会返回。"""
    for mode, fname in redirs:
        if mode == "<":
            fd = os.open(fname, os.O_RDONLY)
            os.dup2(fd, 0)            # stdin 换成文件
        else:
            flags = os.O_WRONLY | os.O_CREAT
            flags |= os.O_APPEND if mode == ">>" else os.O_TRUNC
            fd = os.open(fname, flags, 0o644)
            os.dup2(fd, 1)            # stdout 换成文件
        os.close(fd)
    try:
        os.execvp(argv[0], argv)      # 映像替换；成功则此生代码到此为止
    except FileNotFoundError:
        print(f"{argv[0]}: command not found", file=sys.stderr)
    except PermissionError:
        print(f"{argv[0]}: permission denied", file=sys.stderr)
    os._exit(127)                     # exec 失败，按惯例退出码 127


def run_pipeline(commands, redirs):
    """n 个命令 → n 个子进程 + n-1 根管道，父进程 waitpid 全部回收。"""
    global LAST_STATUS
    n = len(commands)

    # 单命令且是内建：在 shell 自己身体里执行（见 bi_cd 的注释）
    if n == 1 and commands[0] and commands[0][0] in BUILTINS and not redirs:
        LAST_STATUS = BUILTINS[commands[0][0]](commands[0][1:])
        return

    prev_read = None   # 上一根管道的读端
    pids = []

    for i, argv in enumerate(commands):
        if not argv:
            continue
        # 不是最后一个命令就先造一根管道，供它把 stdout 写进来
        read_fd, write_fd = os.pipe() if i < n - 1 else (None, None)

        pid = os.fork()
        if pid == 0:
            # ===== 子进程：fork/exec 之间的那个"窗口" =====
            signal.signal(signal.SIGINT, signal.SIG_DFL)  # Ctrl-C 要杀我，别杀 shell
            if prev_read is not None:
                os.dup2(prev_read, 0)      # stdin ← 上一根管道
            if write_fd is not None:
                os.dup2(write_fd, 1)       # stdout → 这一根管道
            for fd in (prev_read, read_fd, write_fd):
                if fd is not None:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
            # 重定向只属于它跟着的那个命令（真实 shell 里绑定更细，这里简化给末尾命令）
            child_exec(argv, redirs if i == n - 1 else [])
            # child_exec 正常不会返回

        # ===== 父进程：关掉自己用不到的管道端，防止子进程收不到 EOF =====
        if prev_read is not None:
            os.close(prev_read)
        if write_fd is not None:
            os.close(write_fd)
        prev_read = read_fd
        pids.append(pid)

    # 回收所有子进程；$? 取最后一个命令的退出码（与 bash 一致）
    for pid in pids:
        _, status = os.waitpid(pid, 0)
        if os.WIFEXITED(status):
            LAST_STATUS = os.WEXITSTATUS(status)
        elif os.WIFSIGNALED(status):
            LAST_STATUS = 128 + os.WTERMSIG(status)  # 被信号杀死：128+信号编号


# ---------- v1: REPL 主循环 ----------
# read → parse → fork → exec → waitpid，然后循环。

def main():
    global LAST_STATUS
    # shell 自己忽略 Ctrl-C：SIGINT 由终端驱动发给"前台进程组"，
    # shell 不参与挨打，只负责在子进程死后继续打印提示符。
    signal.signal(signal.SIGINT, signal.SIG_IGN)

    while True:
        try:
            line = input(f"\033[1;32mmini\033[0m:\033[1;34m{os.getcwd()}\033[0m$ ")
        except EOFError:      # Ctrl-D
            print()
            break
        if not line.strip():
            continue
        try:
            commands, redirs = parse(line)
            run_pipeline(commands, redirs)
        except (SyntaxError, ValueError) as e:
            print(f"minishell: {e}", file=sys.stderr)
            LAST_STATUS = 2  # 解析失败惯例退出码 2


if __name__ == "__main__":
    main()