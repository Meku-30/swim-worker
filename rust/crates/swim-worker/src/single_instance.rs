//! OS のファイルロックで、同じマシンでの多重起動を防ぐ (Python の single_instance.py と同じ)。
//! ロックファイルには PID を書く (目安。ロックは OS が持つ)
use std::fs::{File, OpenOptions};
use std::io::{Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};

use fs4::fs_std::FileExt;
use thiserror::Error;

#[derive(Debug, Error)]
pub enum AcquireError {
    #[error("別の swim-worker プロセスが既に起動しています (lock: {0})")]
    AlreadyRunning(PathBuf),
    #[error("ロックファイルを作れません ({path}): {source}")]
    Io { path: PathBuf, source: std::io::Error },
}

/// 持っている間ロックが続く。落とすと放す (プロセスが終われば OS も放す)
#[derive(Debug)]
pub struct InstanceLock {
    file: File,
}

impl Drop for InstanceLock {
    fn drop(&mut self) {
        let _ = FileExt::unlock(&self.file);
    }
}

pub fn acquire_at(path: &Path) -> Result<InstanceLock, AcquireError> {
    let io = |source| AcquireError::Io { path: path.to_path_buf(), source };
    if let Some(dir) = path.parent() {
        std::fs::create_dir_all(dir).map_err(io)?;
    }
    let mut file = OpenOptions::new().create(true).read(true).write(true).truncate(false).open(path).map_err(io)?;
    match file.try_lock_exclusive() {
        Ok(true) => {}
        Ok(false) => return Err(AcquireError::AlreadyRunning(path.to_path_buf())),
        Err(e) => return Err(io(e)),
    }
    let _ = file.set_len(0);
    let _ = file.seek(SeekFrom::Start(0));
    let _ = writeln!(file, "{}", std::process::id());
    let _ = file.flush();
    Ok(InstanceLock { file })
}
