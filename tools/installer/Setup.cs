// Setup.exe - installs varsha into C:\Varsha from the "payload" folder next to this exe.
//
//   Setup.exe                              window: pick the folder, Install, start varsha
//   Setup.exe /quiet [/target DIR] [/noshortcuts] [/log FILE]   no window (used by tools\deploy.py)
//
// It copies Varsha.exe, _internal\ (replaced as a whole) and bin\ (new and changed files only) and
// never touches config\, data\, www\, logs\ or etc\: on a fresh folder varsha creates those itself
// on first start. Nothing else is needed on the PC: Python, the web panel and the Visual C++
// runtimes all come with the payload. Needs only the .NET Framework 4 that ships with Windows.
// Build with tools\installer\build.cmd.

using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Windows.Forms;

[assembly: AssemblyTitle("varsha setup")]
[assembly: AssemblyDescription("Installs the varsha local dev stack")]
[assembly: AssemblyProduct("varsha")]
[assembly: AssemblyVersion("0.1.0.0")]
[assembly: AssemblyFileVersion("0.1.0.0")]

internal static class Installer
{
    public const string DefaultTarget = @"C:\Varsha";

    // Written by varsha at runtime; each install keeps its own.
    private static readonly HashSet<string> BinSkip = new HashSet<string>(StringComparer.OrdinalIgnoreCase)
    {
        @"phpmyadmin\config.inc.php", @"phpmyadmin\config.footer.inc.php",
    };

    [STAThread]
    private static int Main(string[] args)
    {
        string payload = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "payload");
        string target = DefaultTarget, log = null;
        bool quiet = false, shortcuts = true;
        for (int i = 0; i < args.Length; i++)
        {
            string a = args[i].ToLowerInvariant();
            if (a == "/quiet") quiet = true;
            else if (a == "/noshortcuts") shortcuts = false;
            else if (a == "/target" && i + 1 < args.Length) target = args[++i];
            else if (a == "/log" && i + 1 < args.Length) log = args[++i];
        }
        if (quiet)
        {
            var text = new StringBuilder();
            int code = Install(payload, target, shortcuts, m => text.AppendLine(m), (done, total) => { });
            if (log != null) File.WriteAllText(log, text.ToString(), Encoding.UTF8);
            return code;
        }
        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);
        var form = new SetupForm(payload, target);
        Application.Run(form);
        return form.ExitCode;
    }

    /// <summary>Returns 0 on success, 1 on failure. `say` gets progress messages.</summary>
    public static int Install(string payload, string target, bool shortcuts, Action<string> say,
                              Action<long, long> progress)
    {
        try
        {
            target = Path.GetFullPath(target.Trim()).TrimEnd('\\');
            if (!File.Exists(Path.Combine(payload, "Varsha.exe")) || !Directory.Exists(Path.Combine(payload, "_internal")))
                throw new Exception("The payload folder is missing or incomplete:\n" + payload +
                                    "\nRebuild it on the development PC with tools\\deploy.cmd.");
            if (Path.GetFullPath(payload).TrimEnd('\\').Equals(target, StringComparison.OrdinalIgnoreCase))
                throw new Exception("The target can't be the payload folder itself.");

            List<string> busy = RunningFrom(target);
            if (busy.Count > 0)
                throw new Exception("These still run from " + target + ":\n  " + string.Join("\n  ", busy) +
                                    "\nQuit varsha (tray > Quit, stop the services) and try again.");

            say("Installing to " + target);
            Directory.CreateDirectory(target);

            // Plan the copy first so the progress bar is honest.
            var plan = new List<KeyValuePair<string, string>>();  // source -> destination
            string internalNew = Path.Combine(target, "_internal.new");
            foreach (string f in Directory.GetFiles(Path.Combine(payload, "_internal"), "*", SearchOption.AllDirectories))
                plan.Add(new KeyValuePair<string, string>(f, Path.Combine(internalNew, Rel(Path.Combine(payload, "_internal"), f))));
            plan.Add(new KeyValuePair<string, string>(Path.Combine(payload, "Varsha.exe"), Path.Combine(target, "Varsha.exe.new")));
            int binSame = 0, binNew = 0;
            string binSrc = Path.Combine(payload, "bin");
            if (Directory.Exists(binSrc))
            {
                foreach (string f in Directory.GetFiles(binSrc, "*", SearchOption.AllDirectories))
                {
                    string rel = Rel(binSrc, f);
                    if (BinSkip.Contains(rel)) continue;
                    string dest = Path.Combine(target, "bin", rel);
                    if (SameFile(f, dest)) { binSame++; continue; }
                    plan.Add(new KeyValuePair<string, string>(f, dest));
                    binNew++;
                }
            }
            long total = 0, done = 0;
            foreach (var p in plan) total += new FileInfo(p.Key).Length;
            say(string.Format("Copying {0} files ({1:N0} MB); {2} files in bin are already up to date",
                              plan.Count, total / 1048576.0, binSame));

            if (Directory.Exists(internalNew)) Directory.Delete(internalNew, true);
            foreach (var p in plan)
            {
                Directory.CreateDirectory(Path.GetDirectoryName(p.Value));
                File.Copy(p.Key, p.Value, true);  // CopyFile keeps the last-write time
                done += new FileInfo(p.Key).Length;
                progress(done, total);
            }

            // Swap the new app in only after everything copied.
            string internalDir = Path.Combine(target, "_internal"), internalOld = Path.Combine(target, "_internal.old");
            if (Directory.Exists(internalOld)) Directory.Delete(internalOld, true);
            if (Directory.Exists(internalDir)) Directory.Move(internalDir, internalOld);
            Directory.Move(internalNew, internalDir);
            if (Directory.Exists(internalOld)) Directory.Delete(internalOld, true);
            string exe = Path.Combine(target, "Varsha.exe");
            if (File.Exists(exe)) File.Delete(exe);
            File.Move(Path.Combine(target, "Varsha.exe.new"), exe);
            say(string.Format("App: Varsha.exe + _internal. bin: {0} copied, {1} unchanged.", binNew, binSame));

            if (shortcuts)
            {
                MakeShortcut(Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory), "Varsha.lnk"), exe, target);
                MakeShortcut(Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.Programs), "Varsha.lnk"), exe, target);
                say("Shortcuts: Desktop and Start menu.");
            }
            foreach (string kept in new[] { "config", "data", "www" })
                if (Directory.Exists(Path.Combine(target, kept))) say("Kept your existing " + kept + "\\ folder.");
            say("Done. Start " + exe);
            return 0;
        }
        catch (Exception ex)
        {
            say("FAILED: " + ex.Message);
            return 1;
        }
    }

    private static string Rel(string root, string path)
    {
        return path.Substring(root.TrimEnd('\\').Length + 1);
    }

    private static bool SameFile(string source, string dest)
    {
        var d = new FileInfo(dest);
        if (!d.Exists) return false;
        var s = new FileInfo(source);
        return s.Length == d.Length && Math.Abs((s.LastWriteTimeUtc - d.LastWriteTimeUtc).TotalSeconds) < 2;
    }

    private static void MakeShortcut(string lnk, string exe, string workDir)
    {
        Type shellType = Type.GetTypeFromProgID("WScript.Shell");
        dynamic shell = Activator.CreateInstance(shellType);
        dynamic link = shell.CreateShortcut(lnk);
        link.TargetPath = exe;
        link.WorkingDirectory = workDir;
        link.IconLocation = exe + ",0";
        link.Description = "varsha local web dev stack";
        link.Save();
        Marshal.FinalReleaseComObject(link);
        Marshal.FinalReleaseComObject(shell);
    }

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern IntPtr OpenProcess(uint access, bool inherit, int pid);

    [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    private static extern bool QueryFullProcessImageName(IntPtr process, int flags, StringBuilder name, ref int size);

    [DllImport("kernel32.dll")]
    private static extern bool CloseHandle(IntPtr handle);

    /// <summary>Processes whose exe lives inside the target (varsha, httpd, mysqld, ...).</summary>
    public static List<string> RunningFrom(string target)
    {
        var found = new List<string>();
        string prefix = target.TrimEnd('\\') + "\\";
        foreach (Process p in Process.GetProcesses())
        {
            IntPtr h = OpenProcess(0x1000 /* PROCESS_QUERY_LIMITED_INFORMATION */, false, p.Id);
            if (h == IntPtr.Zero) continue;
            try
            {
                var sb = new StringBuilder(1024);
                int size = sb.Capacity;
                if (QueryFullProcessImageName(h, 0, sb, ref size) &&
                    sb.ToString().StartsWith(prefix, StringComparison.OrdinalIgnoreCase))
                    found.Add(p.ProcessName + ".exe (PID " + p.Id + ")");
            }
            finally { CloseHandle(h); }
        }
        return found;
    }
}

internal sealed class SetupForm : Form
{
    public int ExitCode = 1;
    private readonly string payload;
    private readonly TextBox targetBox = new TextBox();
    private readonly CheckBox shortcutsBox = new CheckBox();
    private readonly CheckBox startBox = new CheckBox();
    private readonly ProgressBar bar = new ProgressBar();
    private readonly TextBox logBox = new TextBox();
    private readonly Button installButton = new Button();
    private readonly Button closeButton = new Button();

    public SetupForm(string payload, string target)
    {
        this.payload = payload;
        Text = "varsha setup";
        FormBorderStyle = FormBorderStyle.FixedDialog;
        MaximizeBox = false;
        StartPosition = FormStartPosition.CenterScreen;
        ClientSize = new Size(560, 380);
        Font = new Font("Segoe UI", 9.5f);
        try { Icon = Icon.ExtractAssociatedIcon(Assembly.GetExecutingAssembly().Location); } catch { }

        var title = new Label { Text = "varsha", Font = new Font("Segoe UI", 18f, FontStyle.Bold), AutoSize = true, Location = new Point(16, 12) };
        var sub = new Label { Text = "Local web dev stack: Apache, PHP, MySQL/MariaDB, phpMyAdmin, Mailpit", AutoSize = true, Location = new Point(18, 50) };
        var folderLabel = new Label { Text = "Install folder:", AutoSize = true, Location = new Point(18, 86) };
        targetBox.Text = target;
        targetBox.SetBounds(110, 82, 340, 24);
        var browse = new Button { Text = "Browse...", Location = new Point(458, 81), Size = new Size(86, 27) };
        browse.Click += (s, e) =>
        {
            using (var dlg = new FolderBrowserDialog { SelectedPath = targetBox.Text, Description = "Folder for varsha" })
                if (dlg.ShowDialog(this) == DialogResult.OK) targetBox.Text = dlg.SelectedPath;
        };
        shortcutsBox.Text = "Desktop and Start menu shortcuts";
        shortcutsBox.Checked = true;
        shortcutsBox.SetBounds(110, 114, 300, 24);
        startBox.Text = "Start varsha when done";
        startBox.Checked = true;
        startBox.SetBounds(110, 138, 300, 24);
        bar.SetBounds(18, 172, 526, 18);
        logBox.Multiline = true;
        logBox.ReadOnly = true;
        logBox.ScrollBars = ScrollBars.Vertical;
        logBox.SetBounds(18, 198, 526, 132);
        logBox.Text = "Your config, databases (data\\) and sites (www\\) in that folder are never touched." + Environment.NewLine;
        installButton.Text = "Install";
        installButton.SetBounds(368, 340, 86, 30);
        installButton.Click += (s, e) => StartInstall();
        closeButton.Text = "Close";
        closeButton.SetBounds(458, 340, 86, 30);
        closeButton.Click += (s, e) => Close();
        AcceptButton = installButton;
        Controls.AddRange(new Control[] { title, sub, folderLabel, targetBox, browse, shortcutsBox, startBox, bar, logBox, installButton, closeButton });
    }

    private void Say(string message)
    {
        if (InvokeRequired) { BeginInvoke(new Action<string>(Say), message); return; }
        logBox.AppendText(message + Environment.NewLine);
    }

    private void StartInstall()
    {
        installButton.Enabled = closeButton.Enabled = targetBox.Enabled = false;
        string target = targetBox.Text;
        bool shortcuts = shortcutsBox.Checked, start = startBox.Checked;
        var worker = new Thread(() =>
        {
            int code = Installer.Install(payload, target, shortcuts, Say, (done, total) =>
                BeginInvoke(new Action(() => bar.Value = total > 0 ? (int)(done * 100 / total) : 100)));
            BeginInvoke(new Action(() =>
            {
                ExitCode = code;
                closeButton.Enabled = true;
                installButton.Enabled = targetBox.Enabled = code != 0;
                if (code == 0)
                {
                    bar.Value = 100;
                    if (start)
                    {
                        string exe = Path.Combine(Path.GetFullPath(target.Trim()), "Varsha.exe");
                        Process.Start(new ProcessStartInfo(exe) { WorkingDirectory = Path.GetDirectoryName(exe), UseShellExecute = true });
                        Say("varsha started: look for its tray icon; the control panel opens in your browser.");
                    }
                }
            }));
        }) { IsBackground = true };
        worker.Start();
    }
}
