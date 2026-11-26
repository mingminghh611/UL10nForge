using System;
using System.IO;
using System.Reflection;
using BepInEx;

namespace UL10nForge.FontGuard
{
    [BepInPlugin("com.ul10nforge.fontguard", "UL10nForge FontGuard", "1.0.1")]
    [BepInDependency("gravydevsupreme.xunity.autotranslator", BepInDependency.DependencyFlags.SoftDependency)]
    public sealed class FontGuardMonoPlugin : BaseUnityPlugin
    {
        private bool _ranInitial;
        private int _framesWaited;
        private int _periodicChecks;
        private DateTime _nextCheckUtc = DateTime.MinValue;

        private void Start()
        {
            try
            {
                string cfgPath = Path.Combine(Paths.ConfigPath, "ul10n_fontguard.ini");
                FontGuardConfig cfg = FontGuardConfig.Load(cfgPath, Paths.GameRootPath);
                FontGuardCore.Initialize(
                    cfg,
                    delegate(string msg) { Logger.LogInfo(msg); },
                    delegate(string msg) { Logger.LogWarning(msg); }
                );
                FontGuardCore.RunGuardPass(true);
                _ranInitial = true;
                _nextCheckUtc = DateTime.UtcNow.AddSeconds(2.0);
                HookSceneLoaded();
            }
            catch (Exception ex)
            {
                Logger.LogWarning("[UL10nForge.FontGuard] Start skipped: " + ex.Message);
            }
        }

        private void Update()
        {
            if (!_ranInitial)
            {
                _framesWaited++;
                if (_framesWaited >= 5)
                {
                    _ranInitial = true;
                    FontGuardCore.RunGuardPass(true);
                    _nextCheckUtc = DateTime.UtcNow.AddSeconds(2.0);
                }
                return;
            }
            if (_periodicChecks < 3 && DateTime.UtcNow >= _nextCheckUtc)
            {
                _periodicChecks++;
                _nextCheckUtc = DateTime.UtcNow.AddSeconds(3.0);
                FontGuardCore.RunGuardPass(false);
            }
        }

        private void HookSceneLoaded()
        {
            try
            {
                Type smType = FontGuardCore.FindType("UnityEngine.SceneManagement.SceneManager");
                if (smType == null) return;
                MethodInfo addMi = smType.GetMethod("add_sceneLoaded", BindingFlags.Public | BindingFlags.Static);
                if (addMi == null) return;
                ParameterInfo[] ps = addMi.GetParameters();
                if (ps.Length != 1) return;
                Type unityActionType = ps[0].ParameterType;
                Type[] genArgs = unityActionType.GetGenericArguments();
                if (genArgs == null || genArgs.Length != 2) return;
                MethodInfo handler = typeof(FontGuardMonoPlugin).GetMethod("OnSceneLoadedGeneric", BindingFlags.NonPublic | BindingFlags.Static).MakeGenericMethod(genArgs);
                Delegate del = Delegate.CreateDelegate(unityActionType, handler);
                addMi.Invoke(null, new object[] { del });
            }
            catch
            {
            }
        }

        private static void OnSceneLoadedGeneric<TScene, TMode>(TScene scene, TMode mode)
        {
            FontGuardCore.RunGuardPass(false);
        }
    }
}
