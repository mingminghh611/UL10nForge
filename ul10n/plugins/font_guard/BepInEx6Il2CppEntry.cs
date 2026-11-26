using System;
using System.IO;
using System.Reflection;
using BepInEx;
using BepInEx.Unity.IL2CPP;
using Il2CppInterop.Runtime;

namespace UL10nForge.FontGuard
{
    [BepInPlugin("com.ul10nforge.fontguard", "UL10nForge FontGuard", "1.0.1")]
    [BepInDependency("gravydevsupreme.xunity.autotranslator", BepInDependency.DependencyFlags.SoftDependency)]
    public sealed class FontGuardIl2CppPlugin : BasePlugin
    {
        public override void Load()
        {
            try
            {
                string cfgPath = Path.Combine(Paths.ConfigPath, "ul10n_fontguard.ini");
                FontGuardConfig cfg = FontGuardConfig.Load(cfgPath, Paths.GameRootPath);
                FontGuardCore.Initialize(
                    cfg,
                    delegate(string msg) { Log.LogInfo(msg); },
                    delegate(string msg) { Log.LogWarning(msg); }
                );
                FontGuardCore.RunGuardPass(true);
                HookSceneLoaded();
            }
            catch (Exception ex)
            {
                Log.LogWarning("[UL10nForge.FontGuard] Load skipped: " + ex.Message);
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

                MethodInfo handler = typeof(FontGuardIl2CppPlugin)
                    .GetMethod("OnSceneLoadedGeneric", BindingFlags.NonPublic | BindingFlags.Static)
                    .MakeGenericMethod(genArgs);
                Type actionType = typeof(Action<,>).MakeGenericType(genArgs);
                Delegate managedDel = Delegate.CreateDelegate(actionType, handler);

                MethodInfo convertMi = typeof(DelegateSupport)
                    .GetMethod("ConvertDelegate", BindingFlags.Public | BindingFlags.Static)
                    .MakeGenericMethod(unityActionType);
                object il2cppDel = convertMi.Invoke(null, new object[] { managedDel });
                addMi.Invoke(null, new object[] { il2cppDel });
            }
            catch (Exception ex)
            {
                Log.LogWarning("[UL10nForge.FontGuard] SceneLoaded hook skipped: " + ex.Message);
            }
        }

        private static void OnSceneLoadedGeneric<TScene, TMode>(TScene scene, TMode mode)
        {
            FontGuardCore.RunGuardPass(false);
        }
    }
}
