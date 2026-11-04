// Varsha.exe - double-click launcher for varsha (no console window).
//
// Starts  <folder of this exe>\.venv\Scripts\pythonw.exe -m varsha  and exits. varsha itself
// keeps a single instance: launching again while it runs just reopens the control panel.
// Build with tools\launcher\build.cmd (uses the C# compiler that ships with Windows).

using System;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Windows.Forms;

[assembly: AssemblyTitle("varsha")]
[assembly: AssemblyDescription("varsha local dev stack")]
[assembly: AssemblyProduct("varsha")]
[assembly: AssemblyVersion("0.1.0.0")]
[assembly: AssemblyFileVersion("0.1.0.0")]

internal static class Launcher
{
    [STAThread]
    private static int Main(string[] args)
    {
        string root = Path.GetDirectoryName(Assembly.GetExecutingAssembly().Location);
        string pythonw = Path.Combine(root, ".venv", "Scripts", "pythonw.exe");
        if (!File.Exists(pythonw))
        {
            MessageBox.Show(
                "varsha's Python environment is missing:\n" + pythonw + "\n\n" +
                "Recreate it from a terminal in " + root + ":\n" +
                "py -3.12 -m venv .venv\n.venv\\Scripts\\python -m pip install -e .",
                "varsha", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }

        var start = new ProcessStartInfo
        {
            FileName = pythonw,
            Arguments = "-m varsha " + string.Join(" ", args.Select(Quote)),
            WorkingDirectory = root,
            UseShellExecute = false,
            CreateNoWindow = true,
        };

        Process process;
        try
        {
            process = Process.Start(start);
        }
        catch (Exception exc)
        {
            MessageBox.Show("Could not start varsha:\n" + exc.Message, "varsha", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }

        // If varsha dies right away (a broken config, a missing package), say so instead of
        // vanishing silently. A quick *clean* exit is normal: it means varsha was already
        // running and has just reopened its control panel.
        if (process != null && process.WaitForExit(4000) && process.ExitCode != 0)
        {
            string log = Path.Combine(root, "logs", "varsha.log");
            string tail = File.Exists(log) ? string.Join("\n", File.ReadAllLines(log).Reverse().Take(8).Reverse()) : "(no log yet)";
            MessageBox.Show("varsha stopped while starting (exit code " + process.ExitCode + ").\n\nLast lines of logs\\varsha.log:\n" + tail,
                "varsha", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return process.ExitCode;
        }
        return 0;
    }

    private static string Quote(string arg)
    {
        return arg.IndexOfAny(new[] { ' ', '"' }) < 0 ? arg : "\"" + arg.Replace("\"", "\\\"") + "\"";
    }
}
