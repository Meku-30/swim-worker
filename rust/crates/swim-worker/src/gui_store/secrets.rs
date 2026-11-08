//! 資格情報マネージャー (Windows の Generic の資格情報)。
//! 書き方は Python の keyring (WinVaultKeyring、keyring 25.7.0) と同じ: 1 つ目はターゲット名 `swim-worker`、
//! 同じサービスにもう 1 つ書くと前のものを `<ユーザー名>@swim-worker` に動かす。読むときはユーザー名で照合する。
//! こうしておくと、Rust 版が書いたパスワードを Python 版 (巻き戻し・出し直し) が読める
use std::sync::Mutex;

/// 資格情報の読み書きの下回り (Windows は CredReadW などを呼ぶ。テストは偽物)
pub trait CredBackend {
    /// (ユーザー名, 値)。無ければ None
    fn read(&self, target: &str) -> std::io::Result<Option<(String, String)>>;
    fn write(&self, target: &str, user: &str, value: &str) -> std::io::Result<()>;
    /// 無くてもエラーにしない
    fn delete(&self, target: &str) -> std::io::Result<()>;
}

/// keyring の WinVaultKeyring と同じ手順
pub struct Keyring<'a> {
    cred: &'a dyn CredBackend,
}

fn compound(user: &str, service: &str) -> String {
    format!("{user}@{service}")
}

impl<'a> Keyring<'a> {
    pub fn new(cred: &'a dyn CredBackend) -> Self {
        Self { cred }
    }

    pub fn get_password(&self, service: &str, user: &str) -> std::io::Result<Option<String>> {
        let mut res = self.cred.read(service)?;
        if res.as_ref().is_none_or(|(u, _)| !user.is_empty() && u != user) {
            res = self.cred.read(&compound(user, service))?;
        }
        Ok(res.map(|(_, v)| v))
    }

    pub fn set_password(&self, service: &str, user: &str, value: &str) -> std::io::Result<()> {
        if let Some((eu, ev)) = self.cred.read(service)? {
            // 前のものを <ユーザー名>@<サービス> に書き直す
            self.cred.write(&compound(&eu, service), &eu, &ev)?;
        }
        self.cred.write(service, user, value)
    }

    pub fn delete_password(&self, service: &str, user: &str) -> std::io::Result<()> {
        let mut deleted = false;
        for target in [service.to_string(), compound(user, service)] {
            if let Some((u, _)) = self.cred.read(&target)? {
                if u == user {
                    deleted = true;
                    self.cred.delete(&target)?;
                }
            }
        }
        if deleted {
            Ok(())
        } else {
            Err(std::io::Error::new(std::io::ErrorKind::NotFound, "パスワードがありません"))
        }
    }
}

/// テスト・Windows 以外用 (メモリの中だけ)
#[derive(Default)]
pub struct FakeCred {
    items: Mutex<Vec<(String, String, String)>>,
}

impl FakeCred {
    /// (ターゲット名, ユーザー名, 値)
    pub fn dump(&self) -> Vec<(String, String, String)> {
        self.items.lock().unwrap_or_else(|e| e.into_inner()).clone()
    }
}

impl CredBackend for FakeCred {
    fn read(&self, target: &str) -> std::io::Result<Option<(String, String)>> {
        Ok(self.dump().into_iter().find(|(t, _, _)| t == target).map(|(_, u, v)| (u, v)))
    }
    fn write(&self, target: &str, user: &str, value: &str) -> std::io::Result<()> {
        let mut g = self.items.lock().unwrap_or_else(|e| e.into_inner());
        g.retain(|(t, _, _)| t != target);
        g.push((target.into(), user.into(), value.into()));
        Ok(())
    }
    fn delete(&self, target: &str) -> std::io::Result<()> {
        self.items.lock().unwrap_or_else(|e| e.into_inner()).retain(|(t, _, _)| t != target);
        Ok(())
    }
}

#[cfg(windows)]
pub use win::WinCred;

#[cfg(windows)]
mod win {
    use super::CredBackend;
    use windows_sys::Win32::Foundation::{GetLastError, ERROR_NOT_FOUND, FILETIME};
    use windows_sys::Win32::Security::Credentials::{
        CredDeleteW, CredFree, CredReadW, CredWriteW, CREDENTIALW, CRED_PERSIST_ENTERPRISE, CRED_TYPE_GENERIC,
    };

    /// Windows の資格情報マネージャー。値は UTF-16LE (keyring と同じ)
    #[derive(Default)]
    pub struct WinCred;

    fn wide(s: &str) -> Vec<u16> {
        s.encode_utf16().chain(std::iter::once(0)).collect()
    }

    fn from_wide(p: *const u16) -> String {
        if p.is_null() {
            return String::new();
        }
        // SAFETY: Windows が返した 0 終わりの文字列
        unsafe {
            let mut n = 0;
            while *p.add(n) != 0 {
                n += 1;
            }
            String::from_utf16_lossy(std::slice::from_raw_parts(p, n))
        }
    }

    impl CredBackend for WinCred {
        fn read(&self, target: &str) -> std::io::Result<Option<(String, String)>> {
            let t = wide(target);
            let mut p: *mut CREDENTIALW = std::ptr::null_mut();
            // SAFETY: t は 0 終わり。成功したら p を CredFree で返す
            unsafe {
                if CredReadW(t.as_ptr(), CRED_TYPE_GENERIC, 0, &mut p) == 0 {
                    if GetLastError() == ERROR_NOT_FOUND {
                        return Ok(None);
                    }
                    return Err(std::io::Error::last_os_error());
                }
                let c = &*p;
                let user = from_wide(c.UserName);
                // 空の値は CredentialBlob が null のことがある
                let blob: &[u8] = if c.CredentialBlob.is_null() || c.CredentialBlobSize == 0 {
                    &[]
                } else {
                    std::slice::from_raw_parts(c.CredentialBlob, c.CredentialBlobSize as usize)
                };
                let u16s: Vec<u16> = blob.as_chunks::<2>().0.iter().map(|b| u16::from_le_bytes(*b)).collect();
                let value = String::from_utf16_lossy(&u16s);
                CredFree(p as *const _);
                Ok(Some((user, value)))
            }
        }

        fn write(&self, target: &str, user: &str, value: &str) -> std::io::Result<()> {
            let mut t = wide(target);
            let mut u = wide(user);
            let mut comment = wide("Stored using python-keyring");
            let mut blob: Vec<u8> = value.encode_utf16().flat_map(u16::to_le_bytes).collect();
            let c = CREDENTIALW {
                Flags: 0,
                Type: CRED_TYPE_GENERIC,
                TargetName: t.as_mut_ptr(),
                Comment: comment.as_mut_ptr(),
                LastWritten: FILETIME { dwLowDateTime: 0, dwHighDateTime: 0 },
                CredentialBlobSize: blob.len() as u32,
                CredentialBlob: blob.as_mut_ptr(),
                Persist: CRED_PERSIST_ENTERPRISE,
                AttributeCount: 0,
                Attributes: std::ptr::null_mut(),
                TargetAlias: std::ptr::null_mut(),
                UserName: u.as_mut_ptr(),
            };
            // SAFETY: c の指す先はこの関数の間生きている
            if unsafe { CredWriteW(&c, 0) } == 0 {
                return Err(std::io::Error::last_os_error());
            }
            Ok(())
        }

        fn delete(&self, target: &str) -> std::io::Result<()> {
            let t = wide(target);
            // SAFETY: t は 0 終わり
            unsafe {
                if CredDeleteW(t.as_ptr(), CRED_TYPE_GENERIC, 0) == 0 && GetLastError() != ERROR_NOT_FOUND {
                    return Err(std::io::Error::last_os_error());
                }
            }
            Ok(())
        }
    }
}
