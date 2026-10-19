"""User-scoped Windows Credential Manager; JSON/configs contain references only."""
import ctypes
from ctypes import wintypes as w
import json
import os
import re
import uuid


class CredentialError(ValueError):
    pass


def validate_key(value):
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 2200 or any(c.isspace() or ord(c) < 32 for c in value):
        raise CredentialError("API Key 格式不正确，请粘贴完整密钥，不要带空格或换行。")
    return value


def valid_reference(value):
    return isinstance(value, str) and re.fullmatch(r"sr-[a-f0-9]{32}", value) is not None


class WindowsVault:
    def __init__(self):
        if os.name != "nt":
            raise CredentialError("此版本的本机密钥保存需要 Windows 凭据管理器；也可使用高级环境变量配置。")
        class Credential(ctypes.Structure):
            _fields_ = [("Flags",w.DWORD),("Type",w.DWORD),("TargetName",w.LPWSTR),
                        ("Comment",w.LPWSTR),("LastWritten",w.FILETIME),("CredentialBlobSize",w.DWORD),
                        ("CredentialBlob",ctypes.POINTER(w.BYTE)),("Persist",w.DWORD),
                        ("AttributeCount",w.DWORD),("Attributes",ctypes.c_void_p),
                        ("TargetAlias",w.LPWSTR),("UserName",w.LPWSTR)]
        self.Credential = Credential
        self.api = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
        self.api.CredWriteW.argtypes = [ctypes.POINTER(Credential),w.DWORD]
        self.api.CredWriteW.restype = w.BOOL
        self.api.CredReadW.argtypes = [w.LPCWSTR,w.DWORD,w.DWORD,ctypes.POINTER(ctypes.POINTER(Credential))]
        self.api.CredReadW.restype = w.BOOL
        self.api.CredDeleteW.argtypes = [w.LPCWSTR,w.DWORD,w.DWORD]
        self.api.CredDeleteW.restype = w.BOOL
        self.api.CredFree.argtypes = [ctypes.c_void_p]

    @staticmethod
    def target(reference):
        if not valid_reference(reference):
            raise CredentialError("本机密钥引用格式不正确。")
        return "StageRelay/API/" + reference

    def put(self, reference, url, key):
        target = self.target(reference)
        value = json.dumps({"url":url.rstrip("/"),"key":validate_key(key)},ensure_ascii=False).encode("utf-8")
        if len(value) > 2560:
            raise CredentialError("API 地址与密钥超出 Windows 凭据存储大小，请检查输入。")
        blob = (w.BYTE * len(value)).from_buffer_copy(value)
        item = self.Credential(Type=1,TargetName=target,Comment="Stage Relay API key",
                               CredentialBlobSize=len(value),CredentialBlob=blob,Persist=2,UserName="StageRelay")
        try:
            if not self.api.CredWriteW(ctypes.byref(item),0):
                raise CredentialError("Windows 无法安全保存密钥，请在当前用户的正常 Windows 会话中重开面板。")
        finally:
            ctypes.memset(blob,0,len(value))

    def get(self, reference, url):
        pointer = ctypes.POINTER(self.Credential)()
        if not self.api.CredReadW(self.target(reference),1,0,ctypes.byref(pointer)):
            raise CredentialError("本机保存的密钥无法读取，请在接口设置中重新填写并保存。")
        try:
            value = json.loads(ctypes.string_at(pointer.contents.CredentialBlob,pointer.contents.CredentialBlobSize))
            if value.get("url") != url.rstrip("/"):
                raise CredentialError("接口地址已变更，请为新地址重新填写密钥。")
            return validate_key(value.get("key"))
        except (ValueError, TypeError, AttributeError) as error:
            if isinstance(error,CredentialError):
                raise
            raise CredentialError("本机密钥记录无法读取，请重新填写并保存。") from None
        finally:
            self.api.CredFree(pointer)

    def delete(self, reference):
        if not self.api.CredDeleteW(self.target(reference),1,0) and ctypes.get_last_error() != 1168:
            raise CredentialError("Windows 无法移除本机密钥记录。")


def new_reference():
    return "sr-" + uuid.uuid4().hex
