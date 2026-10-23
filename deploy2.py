# -*- coding: utf-8 -*-
"""
把更新后的 server.py + index.html 部署到测试机 82.156.200.181 并重启服务。
凭证从 C:/Users/陈俊惠/Documents/82.156.200.181.txt 运行时读取，不明文写入本文件。
pymysql 包上次已上传到远端，本次只需覆盖两个改动文件。
"""
import os, re, sys, time
import paramiko

CRED_FILE = r"C:/Users/陈俊惠/Documents/82.156.200.181.txt"
LOCAL_DIR = os.path.dirname(os.path.abspath(__file__))
REMOTE_DIR = "/opt/market_h5"
FILES = ["server.py", "index.html"]


def load_creds():
    """凭证文件格式（实测）：
        ssh 端口号 10086
        用户名：opencode
        密码：xxxx
    """
    txt = open(CRED_FILE, "r", encoding="utf-8", errors="ignore").read()
    host = "82.156.200.181"  # 文件名即 IP
    m = re.search(r"(?:端口|port)\D{0,6}(\d{2,5})", txt, re.I)
    port = int(m.group(1)) if m else 22
    user = re.search(r"用户名\s*[:：=]?\s*([A-Za-z0-9_.\-]+)", txt)
    pwd = re.search(r"密码\s*[:：=]?\s*(\S+)", txt)
    return host, port, user.group(1), pwd.group(1).strip()


def main():
    host, port, user, pwd = load_creds()
    print("target: %s@%s:%s" % (user, host, port))
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(host, port=port, username=user, password=pwd, timeout=30)
    sftp = ssh.open_sftp()
    for f in FILES:
        local = os.path.join(LOCAL_DIR, f)
        remote = REMOTE_DIR + "/" + f
        t0 = time.time()
        sftp.put(local, remote)
        print("uploaded %-14s %6.1f KB  %.2fs" % (f, os.path.getsize(local) / 1024.0, time.time() - t0))
    sftp.close()
    # 重启 systemd 服务（sudo -S 从 stdin 读密码）
    def sudo(cmd):
        stdin, stdout, stderr = ssh.exec_command("sudo -S -p '' " + cmd, timeout=90)
        stdin.write(pwd + "\n")
        stdin.flush()
        return (stdout.read() + stderr.read()).decode("utf-8", "ignore").strip()

    print("$ restart market-h5")
    print(sudo("systemctl restart market-h5.service"))
    time.sleep(2)
    print("$ is-active:", sudo("systemctl is-active market-h5.service"))
    print("$ journal:")
    print(sudo("journalctl -u market-h5.service -n 6 --no-pager"))
    ssh.close()
    print("DEPLOY_DONE")


if __name__ == "__main__":
    main()
