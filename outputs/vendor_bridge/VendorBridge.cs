using System;
using System.Collections;
using System.IO;
using System.Reflection;
using System.Text;

internal static class VendorBridge
{
    private static string vendorDirectory;

    private static Assembly ResolveAssembly(object sender, ResolveEventArgs args)
    {
        string name = new AssemblyName(args.Name).Name + ".dll";
        string path = Path.Combine(vendorDirectory, name);
        return File.Exists(path) ? Assembly.LoadFrom(path) : null;
    }

    private static void SetProperty(object target, string name, object value)
    {
        PropertyInfo property = target.GetType().GetProperty(name, BindingFlags.Public | BindingFlags.Instance);
        if (property == null)
            throw new MissingMemberException(target.GetType().FullName, name);
        property.SetValue(target, value, null);
    }

    public static int Main(string[] args)
    {
        vendorDirectory = Environment.GetEnvironmentVariable("ESC_VENDOR_DIR");
        if (String.IsNullOrEmpty(vendorDirectory))
            vendorDirectory = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFilesX86), "ESC", "lib", "Starter");

        try
        {
            Directory.SetCurrentDirectory(vendorDirectory);
            AppDomain.CurrentDomain.AssemblyResolve += ResolveAssembly;

            Assembly cameras = Assembly.LoadFrom(Path.Combine(vendorDirectory, "Cameras.dll"));
            Type cameraType = cameras.GetType("Cameras.CameraSN", true);

            if (args.Length == 1 && (args[0] == "scan-all" || args[0] == "scan-dynacolor"))
            {
                Assembly starterAssembly = Assembly.LoadFrom(Path.Combine(vendorDirectory, "StarterLib.dll"));
                Type starterType = starterAssembly.GetType("StarterLib.Class1", true);
                string[] methods = args[0] == "scan-all"
                    ? new string[] { "Milesight", "TopView", "Hunt", "Sunell", "Unv", "DynaColor" }
                    : new string[] { "DynaColor" };
                foreach (string methodName in methods)
                {
                    object starter = Activator.CreateInstance(starterType);
                    MethodInfo scan = starterType.GetMethod(methodName, BindingFlags.Public | BindingFlags.Instance);
                    if (scan == null)
                        continue;
                    IEnumerable rows = (IEnumerable)scan.Invoke(starter, null);
                    foreach (object row in rows)
                    {
                        string encoded = Convert.ToBase64String(Encoding.UTF8.GetBytes(Convert.ToString(row)));
                        Console.WriteLine("RESULT\t" + methodName.ToLowerInvariant() + "\t" + encoded);
                    }
                }
                return 0;
            }

            if (args.Length == 1 && args[0] == "self-test")
            {
                MethodInfo nativeMethod = cameraType.GetMethod("SetHostNetwork", BindingFlags.NonPublic | BindingFlags.Static);
                MethodInfo changeMethod = cameraType.GetMethod("ChangingNetSettings", BindingFlags.Public | BindingFlags.Instance);
                if (nativeMethod == null || changeMethod == null)
                    throw new MissingMethodException("CameraSN vendor network methods were not found.");
                Assembly starterAssembly = Assembly.LoadFrom(Path.Combine(vendorDirectory, "StarterLib.dll"));
                Type starterType = starterAssembly.GetType("StarterLib.Class1", true);
                string[] methods = new string[] { "Milesight", "TopView", "Hunt", "Sunell", "Unv", "DynaColor" };
                foreach (string methodName in methods)
                    if (starterType.GetMethod(methodName, BindingFlags.Public | BindingFlags.Instance) == null)
                        throw new MissingMethodException(methodName + " vendor discovery method was not found.");
                if (starterType.GetMethod("ChangingNetSettings", BindingFlags.Public | BindingFlags.Instance) == null)
                    throw new MissingMethodException("DynaColor vendor network method was not found.");
                Console.WriteLine("OK x86 bridge; all ESC discovery and network methods are available");
                return 0;
            }

            if (args.Length == 9 && args[0] == "set-dynacolor")
            {
                Assembly starterAssembly = Assembly.LoadFrom(Path.Combine(vendorDirectory, "StarterLib.dll"));
                Type starterType = starterAssembly.GetType("StarterLib.Class1", true);
                object starter = Activator.CreateInstance(starterType);
                MethodInfo changeDynaColor = starterType.GetMethod("ChangingNetSettings", BindingFlags.Public | BindingFlags.Instance);
                changeDynaColor.Invoke(starter, new object[] { args[1], args[2], args[3], args[4], args[5], args[6], args[7], args[8] });
                Console.WriteLine("COMMAND_SENT");
                return 0;
            }

            if (args.Length != 10 || args[0] != "set-sunell")
            {
                Console.Error.WriteLine("Usage: VendorBridge self-test | scan-all | scan-dynacolor | set-sunell ... | set-dynacolor NEW_IP MASK GATEWAY DNS PORT MODEL HOSTNAME MAC");
                return 2;
            }

            object camera = Activator.CreateInstance(cameraType, new object[] { args[6], args[7], args[8] });

            string password = Console.ReadLine() ?? "";
            SetProperty(camera, "IP", args[1]);
            SetProperty(camera, "NetMask", args[3]);
            SetProperty(camera, "GateWay", args[4]);
            SetProperty(camera, "DNS1", args[5]);
            SetProperty(camera, "User", args[9]);
            SetProperty(camera, "Password", password);

            MethodInfo change = cameraType.GetMethod("ChangingNetSettings", BindingFlags.Public | BindingFlags.Instance);
            change.Invoke(camera, new object[] { args[2], args[3], args[4], args[5] });
            Console.WriteLine("COMMAND_SENT");
            return 0;
        }
        catch (TargetInvocationException ex)
        {
            Exception inner = ex.InnerException ?? ex;
            Console.Error.WriteLine(inner.GetType().Name + ": " + inner.Message);
            return 1;
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine(ex.GetType().Name + ": " + ex.Message);
            return 1;
        }
    }
}
