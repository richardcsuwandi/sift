ObjC.import("AppKit");

const system = Application.currentApplication();
system.includeStandardAdditions = true;

const appRoot = "__APP_ROOT__";
const appURL = "http://127.0.0.1:8000";

function shellQuote(value) {
  return "'" + value.replace(/'/g, "'\\''") + "'";
}

// An osacompile applet quits as soon as run() returns, and there is no flag
// that makes one stay open, so its idle() and quit() handlers never get a
// chance to run. Tying the server's lifetime to this app therefore stopped
// the server a moment after starting it, and the browser opened onto a dead
// backend: every request failed with "Failed to fetch".
//
// So this is now only a launcher. It starts a server that outlives it and
// opens the page. Relaunching is safe and simply reopens the tab, because
// start_server.sh reuses a healthy server. Stop the server with
// macos/stop_server.sh.
function run() {
  try {
    system.doShellScript(shellQuote(appRoot + "/macos/start_server.sh"));
  } catch (error) {
    system.displayDialog("Sift could not start.\n\n" + error.message, {
      buttons: ["OK"],
      defaultButton: "OK",
    });
    return;
  }
  system.openLocation(appURL);
}
