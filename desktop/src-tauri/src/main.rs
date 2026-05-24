// Worklist Tracker — native Tauri wrapper.
//
// Responsibilities:
//   1. Pick a free TCP port on 127.0.0.1.
//   2. Spawn the bundled Python interpreter running ``uvicorn`` on that port.
//   3. Poll /healthz until the server reports ready.
//   4. Point the Tauri window at http://127.0.0.1:<port>/ and show it.
//   5. Kill the sidecar when the window closes.
//
// Path resolution: the AppImage's AppRun is expected to export the
// TBDTASK_PYTHON and TBDTASK_APP_DIR env vars before exec'ing the binary,
// so we don't bake any layout assumptions into the Rust code. The
// fallback (used during ``cargo run`` development) is the system
// ``python3`` and the source tree's ``app/`` package.

#![cfg_attr(
    all(not(debug_assertions), target_os = "windows"),
    windows_subsystem = "windows"
)]

use std::net::TcpListener;
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::thread;
use std::time::{Duration, Instant};

use tauri::Manager;

struct Sidecar(Mutex<Option<Child>>);

fn pick_free_port() -> u16 {
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind 127.0.0.1:0");
    listener.local_addr().expect("local_addr").port()
}

fn resolve_python() -> PathBuf {
    if let Ok(p) = std::env::var("TBDTASK_PYTHON") {
        return PathBuf::from(p);
    }
    PathBuf::from("python3")
}

fn resolve_app_dir() -> PathBuf {
    if let Ok(p) = std::env::var("TBDTASK_APP_DIR") {
        return PathBuf::from(p);
    }
    // Development fallback: assume cargo run from desktop/src-tauri, so the
    // FastAPI app sits two levels up.
    let exe = std::env::current_exe().expect("current_exe");
    let mut dir = exe.parent().expect("exe parent").to_path_buf();
    // Walk up looking for an ``app/`` directory.
    for _ in 0..6 {
        if dir.join("app").is_dir() && dir.join("app/main.py").is_file() {
            return dir;
        }
        if !dir.pop() {
            break;
        }
    }
    PathBuf::from(".")
}

fn spawn_sidecar(port: u16) -> std::io::Result<Child> {
    let python = resolve_python();
    let app_dir = resolve_app_dir();
    let site_packages = std::env::var("TBDTASK_SITE_PACKAGES").ok();

    let mut cmd = Command::new(python);
    cmd.current_dir(&app_dir)
        .arg("-m")
        .arg("uvicorn")
        .arg("app.main:app")
        .arg("--host")
        .arg("127.0.0.1")
        .arg("--port")
        .arg(port.to_string())
        .arg("--log-level")
        .arg("warning")
        .env("TBDTASK_SINGLE_TENANT", "1")
        .env("TBDTASK_LAUNCHER", "1")
        .env("TBDTASK_PORT", port.to_string())
        .stdout(Stdio::null())
        .stderr(Stdio::inherit());

    // Append site-packages to PYTHONPATH if AppRun told us where it is.
    if let Some(sp) = site_packages {
        let existing = std::env::var("PYTHONPATH").unwrap_or_default();
        let joined = if existing.is_empty() {
            format!("{}:{}", app_dir.display(), sp)
        } else {
            format!("{}:{}:{}", app_dir.display(), sp, existing)
        };
        cmd.env("PYTHONPATH", joined);
    } else {
        cmd.env("PYTHONPATH", app_dir.display().to_string());
    }

    cmd.spawn()
}

fn wait_for_server(port: u16, timeout: Duration) -> bool {
    let url = format!("http://127.0.0.1:{}/healthz", port);
    let deadline = Instant::now() + timeout;
    while Instant::now() < deadline {
        // We don't pull in an HTTP client; a TCP connect + a tiny GET is
        // enough to know uvicorn is accepting requests. Healthz returns
        // a fixed 200 so any 2xx response counts.
        if let Ok(mut stream) = std::net::TcpStream::connect_timeout(
            &format!("127.0.0.1:{}", port).parse().unwrap(),
            Duration::from_millis(200),
        ) {
            use std::io::{Read, Write};
            let _ = stream.set_read_timeout(Some(Duration::from_millis(500)));
            let _ = stream.write_all(
                format!("GET /healthz HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n").as_bytes(),
            );
            let mut buf = [0u8; 64];
            if let Ok(n) = stream.read(&mut buf) {
                if n > 12 && &buf[..12] == b"HTTP/1.0 200" {
                    return true;
                }
                if n > 12 && &buf[..12] == b"HTTP/1.1 200" {
                    return true;
                }
            }
        }
        let _ = url;
        thread::sleep(Duration::from_millis(150));
    }
    false
}

fn main() {
    let port = pick_free_port();
    let url = format!("http://127.0.0.1:{}/", port);

    let child = spawn_sidecar(port).expect("spawn python sidecar");
    let sidecar = Sidecar(Mutex::new(Some(child)));

    if !wait_for_server(port, Duration::from_secs(20)) {
        eprintln!("tbdtask: sidecar failed to come up on {}", url);
        if let Some(mut c) = sidecar.0.lock().unwrap().take() {
            let _ = c.kill();
        }
        std::process::exit(1);
    }

    tauri::Builder::default()
        .manage(sidecar)
        .setup(move |app| {
            let window = app.get_webview_window("main").expect("main window");
            window.eval(&format!("window.location.replace('{}')", url))?;
            window.show()?;
            Ok(())
        })
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::Destroyed = event {
                if let Some(state) = window.try_state::<Sidecar>() {
                    if let Some(mut c) = state.0.lock().unwrap().take() {
                        let _ = c.kill();
                        let _ = c.wait();
                    }
                }
            }
        })
        .run(tauri::generate_context!())
        .expect("tauri run");
}
