using System;
using System.IO;
using MelonLoader;

[assembly: MelonInfo(typeof(UL10nForge.FontGuard.FontGuardMelonMod), "UL10nForge FontGuard", "1.0.1", "UL10nForge")]
[assembly: MelonGame(null, null)]

namespace UL10nForge.FontGuard
{
    public sealed class FontGuardMelonMod : MelonMod
    {
        public override void OnLateInitializeMelon()
        {
            try
            {
                string gameRoot = AppDomain.CurrentDomain.BaseDirectory ?? "";
                string cfgPath = Path.Combine(Path.Combine(gameRoot, "AutoTranslator"), "ul10n_fontguard.ini");
                FontGuardConfig cfg = FontGuardConfig.Load(cfgPath, gameRoot);
                FontGuardCore.Initialize(
                    cfg,
                    delegate(string msg) { LoggerInstance.Msg(msg); },
                    delegate(string msg) { LoggerInstance.Warning(msg); }
                );
                FontGuardCore.RunGuardPass(true);
            }
            catch (Exception ex)
            {
                LoggerInstance.Warning("[UL10nForge.FontGuard] Init skipped: " + ex.Message);
            }
        }

        public override void OnSceneWasLoaded(int buildIndex, string sceneName)
        {
            FontGuardCore.RunGuardPass(false);
        }
    }
}
