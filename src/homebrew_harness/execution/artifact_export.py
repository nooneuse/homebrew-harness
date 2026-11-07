"""A fixed isolated collector; archive bytes and metadata are untrusted on receipt."""

import base64
import hashlib
import json

from homebrew_harness.execution.artifact_policy import ArtifactError

COLLECTOR = r"""
import os,stat,json,hashlib,base64,sys,signal,time
profile=json.loads(sys.argv[1])
# The namespace contains only this invocation. Freeze residual task writers,
# preserving PID 1 and the collecting exec. A failed freeze aborts export.
for repeat in range(3):
 for entry in os.listdir('/proc'):
  if not entry.isdigit() or int(entry) in {1,os.getpid()}:continue
  try:os.kill(int(entry),signal.SIGSTOP)
  except ProcessLookupError:pass
# Fail closed if the bounded freeze did not reach a quiescent namespace.
for entry in os.listdir('/proc'):
 if not entry.isdigit() or int(entry) in {1,os.getpid()}:continue
 try:
  with open('/proc/'+entry+'/status') as status:
   state=next(line.split()[1] for line in status if line.startswith('State:'))
  if state not in {'T','t','Z','X'}:raise ValueError('artifact writers remain active')
 except FileNotFoundError:pass
root=os.open('/workspace',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
files=[];missing=[];total=0
try:
 for rule in profile['files']:
  parts=rule['source'].split('/');parents=[];current=root
  try:
   try:
    for part in parts[:-1]:
     fd=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=current)
     parents.append(fd);current=fd
    fd=os.open(parts[-1],os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=current)
   except FileNotFoundError:
    missing.append(rule['source']);continue
   try:
    before=os.fstat(fd)
    if not stat.S_ISREG(before.st_mode) or before.st_nlink!=1 or before.st_size>rule['max_bytes']:raise ValueError('artifact type or size rejected')
    chunks=[];size=0
    while True:
     data=os.read(fd,min(65536,rule['max_bytes']-size+1))
     if not data:break
     size+=len(data)
     if size>rule['max_bytes']:raise ValueError('artifact size changed')
     chunks.append(data)
    after=os.fstat(fd)
    version=lambda info:(info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns)
    if version(before)!=version(after):raise ValueError('artifact changed during export')
    data=b''.join(chunks);total+=len(data)
    if total>profile['max_bytes']:raise ValueError('artifact bundle exceeds limit')
    files.append({'source':rule['source'],'bytes':size,'sha256':hashlib.sha256(data).hexdigest(),'data':base64.b64encode(data).decode('ascii')})
   finally:os.close(fd)
  finally:
   for parent in reversed(parents):os.close(parent)
finally:os.close(root)
print(json.dumps({'format':1,'files':files,'missing':missing},separators=(',',':')),flush=True)
"""


def decode_export(raw, profile):
    if len(raw.encode()) > profile.max_bytes * 2 + 65536:
        raise ArtifactError("Artifact transport exceeds its bounded byte limit.")
    try:
        message = json.loads(raw)
    except (ValueError, TypeError):
        raise ArtifactError("Artifact transport is incomplete JSON.") from None
    if (
        not isinstance(message, dict)
        or set(message) != {"format", "files", "missing"}
        or message["format"] != 1
        or not isinstance(message["files"], list)
        or not isinstance(message["missing"], list)
    ):
        raise ArtifactError("Invalid artifact response envelope.")
    rules = {f.source: f for f in profile.files}
    seen = set()
    files = []
    total = 0
    for row in message["files"]:
        if (
            not isinstance(row, dict)
            or set(row) != {"source", "bytes", "sha256", "data"}
            or not isinstance(row["source"], str)
            or row["source"] not in rules
            or row["source"] in seen
        ):
            raise ArtifactError(
                "Artifact response includes an undeclared or duplicate file."
            )
        rule = rules[row["source"]]
        if (
            type(row["bytes"]) is not int
            or not 0 <= row["bytes"] <= rule.max_bytes
            or not isinstance(row["sha256"], str)
            or not isinstance(row["data"], str)
        ):
            raise ArtifactError("Invalid artifact file size.")
        try:
            data = base64.b64decode(row["data"], validate=True)
        except (ValueError, TypeError):
            raise ArtifactError("Invalid artifact encoding.") from None
        total += len(data)
        if (
            len(data) != row["bytes"]
            or hashlib.sha256(data).hexdigest() != row["sha256"]
            or total > profile.max_bytes
        ):
            raise ArtifactError(
                "Artifact contents do not match their bounded manifest."
            )
        seen.add(row["source"])
        files.append((rule, data))
    missing = message["missing"]
    if (
        any(not isinstance(p, str) or p not in rules for p in missing)
        or len(missing) != len(set(missing))
        or set(missing) & seen
        or set(missing) | seen != set(rules)
    ):
        raise ArtifactError("Artifact response omitted or invented declared files.")
    return files, missing
