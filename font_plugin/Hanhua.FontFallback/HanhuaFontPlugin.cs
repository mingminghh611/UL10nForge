using BepInEx;
using HarmonyLib;
using System;
using System.Collections;
using System.Collections.Generic;
using System.IO;
using System.Reflection;
using System.Runtime.CompilerServices;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.RegularExpressions;
using UnityEngine;
using UnityEngine.SceneManagement;

namespace Hanhua.FontFallback
{
    [BepInPlugin(PluginGuid, PluginName, PluginVersion)]
    public sealed class HanhuaFontPlugin : BaseUnityPlugin
    {
        // Rendezvous 实证 2026-08-17：主菜单/游戏内 TMP 文本的字体引用
        // 无法通过对象扫描定位（FindObjectsOfTypeAll 找不到场景 TMP 文本
        // ——类型发现/扫描链在游戏内失效，ui/tmp 恒 6）——Harmony 补丁
        // hook TMP_Text.set_text 与 LocalizationText.ChangeLanguage：文本
        // 每次更新后强制字体=工具字体，100% 覆盖所有渲染路径。
        public static HanhuaFontPlugin Instance;
        private static MethodInfo _tmpSetTextMethod;
        private static MethodInfo _tmpOnEnableMethod;
        private static MethodInfo _tmpGenerateTextMeshMethod;
        private static MethodInfo _locChangeLanguageMethod;
        private static bool _harmonyPatched;
        private sealed class TranslationApplicationState
        {
            public object Target;
            public string LastSource;
            public string LastTarget;
        }

        private sealed class RuntimeTranslationTemplate
        {
            public List<string> slots;
            public List<string> sourceFragments;
            public List<string> targetFragments;
        }

        private enum TranslationMatchMode
        {
            None,
            Exact,
            Normalized,
            Template,
        }

        private enum TranslationTargetKind
        {
            None,
            Tmp,
            Ui,
            UiToolkit,
            TextMesh,
        }

        private sealed class TmpFactoryAttempt
        {
            public MethodInfo Factory;
            public object FirstArgument;
        }

        public const string PluginGuid = "com.hanhua.fontfallback";
        public const string PluginName = "Hanhua Font Fallback";
        // 1.5.0：性能根治（写回后场景卡顿）——诊断 dump 限次 + AllFontProps
        // 全量大扫静默退避 + 扫描节奏放宽（Harmony set_text 钩子已是
        // 动态文本主路径，周期扫描只做兜底）。
        public const string PluginVersion = "1.5.0";

        // Phase 3 协议 v5：逐 scalar 证明（required-glyphs.json 每码点验证）
        private const int HealthProtocolVersion = 5;
        private const int MaxDetailRecords = 256;
        private const int MaxScenes = 64;

        private const uint FrPrivate = 0x10;
        private const float ScanIntervalSeconds = 2f;
        private Font dynamicFont;
        private Font tmpSourceFont;
        private UnityEngine.Object dynamicTmpFont;
        private AssetBundle tmpFontBundle;
        private string tmpFontSource = "";
        private string tmpFontFamily = "";
        private Type tmpFontAssetType;
        private Type tmpSettingsType;
        private Type tmpTextType;
        private Type uiTextType;
        private Type uiToolkitTextElementType;
        private Type uiToolkitDocumentType;
        private readonly List<TmpFactoryAttempt> tmpFactoryAttempts =
            new List<TmpFactoryAttempt>();
        private readonly List<Font> tmpFontCandidates = new List<Font>();
        private int nextTmpFactoryAttempt;
        private bool tmpFactoryVerified;
        private object activeTmpFactoryArgument;
        private string fontPath;
        private string pluginDirectory;
        private readonly Dictionary<string, string> exactTranslations =
            new Dictionary<string, string>(StringComparer.Ordinal);
        private readonly Dictionary<string, string> normalizedTranslations =
            new Dictionary<string, string>(StringComparer.Ordinal);
        private readonly HashSet<string> conflictingNormalizedTranslations =
            new HashSet<string>(StringComparer.Ordinal);
        private readonly List<RuntimeTranslationTemplate> runtimeTemplates =
            new List<RuntimeTranslationTemplate>();
        // W3 运行时排除表（translations-exclude.json）：静态写回被回退
        // （保留原文防断链）的逻辑键原文——插件把这些串再翻译成中文 →
        // 游戏按原名查找断链（按键失灵）。exact 按原文精确匹配；
        // normalized 按 NormalizeRuntimeText 归一后匹配（与翻译查找同
        // 归一规则，排除表里带换行/首尾空格的原文同样被拦）。
        private readonly HashSet<string> excludedTranslations =
            new HashSet<string>(StringComparer.Ordinal);
        private readonly HashSet<string> excludedNormalizedTranslations =
            new HashSet<string>(StringComparer.Ordinal);
        private readonly Dictionary<int, TranslationApplicationState>
            translationApplicationStates =
                new Dictionary<int, TranslationApplicationState>();
        private string legacyStatus = "not_started";
        private string legacyError = "";
        private string tmpStatus = "not_started";
        private string tmpError = "";
        private string uiToolkitStatus = "not_started";
        private string uiToolkitError = "";
        private long totalTmpApplications;
        private long totalUiApplications;
        private long totalImGuiApplications;
        private long totalTextMeshApplications;
        private long totalUiToolkitApplications;
        private long totalExactTranslationApplications;
        private long totalNormalizedTranslationApplications;
        private long totalTemplateTranslationApplications;
        private readonly long[,] translationApplicationsByTargetAndMode =
            new long[5, 4];
        private string lastHealthPayload;
        // ── Phase 3：逐 scalar 证明状态 ──
        private string sessionNonce = "";
        private readonly List<string> seenScenes = new List<string>();
        private string requiredGlyphsHash = "";
        private readonly List<uint> requiredGlyphs = new List<uint>();
        private readonly HashSet<int> legacyCovered = new HashSet<int>();
        private readonly HashSet<int> tmpCovered = new HashSet<int>();
        private readonly List<uint> missingCodepoints = new List<uint>();
        private int verifiedMissingTotal;
        private long glyphLegacyTotal;
        private long glyphLegacyCovered;
        private long glyphLegacyMissing;
        private long glyphTmpTotal;
        private long glyphTmpCovered;
        private long glyphTmpMissing;
        private string glyphVerificationError = "";
        // 消费者统计（看见并覆盖中文文本对象的证明）
        private long consumersDiscovered;
        private long consumersChinese;
        private long consumersCovered;
        private long consumersMissing;
        private long consumersFailed;
        private readonly List<string> consumerFailures =
            new List<string>();

        [DllImport("gdi32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern int AddFontResourceEx(
            string fileName,
            uint flags,
            IntPtr reserved);

        [DllImport("gdi32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern bool RemoveFontResourceEx(
            string fileName,
            uint flags,
            IntPtr reserved);

        private void Awake()
        {
            Instance = this;
            pluginDirectory = Path.Combine(Paths.PluginPath, "HanhuaFont");
            DiscoverOptionalTextTypes();
            sessionNonce = Guid.NewGuid().ToString("N");
            SetupHarmonyPatches();
            try
            {
                LoadRequiredGlyphs();
            }
            catch (Exception exception)
            {
                Logger.LogError(
                    "Required glyph manifest failed; glyph verification disabled: "
                    + exception);
            }
            AddScene(SceneManager.GetActiveScene().name);
            try
            {
                LoadExactTranslations();
            }
            catch (Exception exception)
            {
                Logger.LogError("Translation mapping failed; fonts will continue: " + exception);
            }

            try
            {
                LoadFontPayload();
            }
            catch (Exception exception)
            {
                legacyStatus = "failed";
                legacyError = exception.GetType().Name + ": " + exception.Message;
                tmpStatus = "failed";
                tmpError = legacyError;
                uiToolkitStatus = uiToolkitTextElementType == null
                    || uiToolkitDocumentType == null
                    ? "unsupported"
                    : "failed";
                uiToolkitError = legacyError;
                Logger.LogError("Font payload initialization failed; translations continue: " + exception);
            }

            WriteHealthManifest(false);

            SceneManager.sceneLoaded += OnSceneLoaded;
            SafeApplyFonts("awake");
            StartCoroutine(ScanLoop());
        }

        // Rendezvous 实证 2026-08-17：ScanLoop 协程在场景切换后未再输出
        // periodic 扫描（主线程卡死时协程不跑）——Update 轮询兜底（MonoBehaviour
        // 每帧回调，主线程活着就一定执行）。每 1 秒扫描一次替换场景内
        // 后创建的 TMP/UGUI 文本字体。
        private float _lastUpdateScan = 0f;
        // 1.5.0 性能：周期扫描空转计数（连续 0 应用 → 退避到长间隔；
        // 场景加载/文本更新钩子仍然即时全扫）。
        private int _scansWithNoApply = 0;
        private const float UpdateScanInterval = 1f;
        // 退避后的 Update/periodic 间隔（空转 N 次后进入稳态：游戏正常
        // 渲染期间不做重量级扫描，只靠 Harmony 钩子即时补丁新文本）。
        private const float BackoffUpdateScanInterval = 6f;
        private const float BackoffScanIntervalSeconds = 8f;

        private void Update()
        {
            // Harmony 补丁延迟重试：插件 Awake 早于游戏主程序集加载，
            // LocalizationText 补丁需等 Assembly-CSharp 就绪（每 2 秒
            // 尝试一次，打上即停）
            if (!_harmonyPatched && Time.frameCount % 120 == 0)
            {
                SetupHarmonyPatches();
            }
            if (Time.realtimeSinceStartup - _lastUpdateScan < UpdateInterval())
            {
                return;
            }
            _lastUpdateScan = Time.realtimeSinceStartup;
            SafeApplyFonts("update");
        }

        // 1.5.0 性能：稳态退避间隔（连续空转扫描越多 → 间隔越长，
        // 上限 BackoffUpdateScanInterval）。
        private float UpdateInterval()
        {
            if (_scansWithNoApply <= 0)
            {
                return UpdateScanInterval;
            }
            return System.Math.Min(
                UpdateScanInterval + _scansWithNoApply,
                BackoffUpdateScanInterval);
        }

        // Rendezvous 实证 2026-08-17：IMGUI（OnGUI）文本用 GUI.skin 默认
        // 字体（内置 Arial 无中文字形）→ 口口口。GUI.skin 只能在 OnGUI
        // 生命周期内访问（运行时检查），Update/协程里调用必抛异常跳过
        // （旧实现每帧尝试都失败）——OnGUI 回调是唯一安全上下文，每帧
        // 检查 skin 是否已用动态字体（GUI.skin 在场景重建后重置）。
        private void OnGUI()
        {
            if (dynamicFont == null)
            {
                return;
            }
            try
            {
                UnityEngine.GUISkin skin = UnityEngine.GUI.skin;
                if (skin == null)
                {
                    return;
                }
                bool dirty = skin.font != dynamicFont;
                if (!dirty)
                {
                    foreach (UnityEngine.GUIStyle style in skin)
                    {
                        if (style != null && style.font != null
                            && !ReferenceEquals(style.font, dynamicFont))
                        {
                            dirty = true;
                            break;
                        }
                    }
                }
                if (!dirty)
                {
                    return;
                }
                // Rendezvous 实证 2026-08-17：游戏开发者设置的官方中文字体
                // （font 非 null）缺字形 → 口口。必须**无条件**替换为工具
                // 字体（不能只在 font == null 时替换——那正是之前 IMGUI
                // 口口不变的原因）。
                int applied = 0;
                foreach (UnityEngine.GUIStyle style in skin)
                {
                    if (style != null
                        && !ReferenceEquals(style.font, dynamicFont))
                    {
                        style.font = dynamicFont;
                        applied++;
                    }
                }
                if (!ReferenceEquals(skin.font, dynamicFont))
                {
                    skin.font = dynamicFont;
                    applied++;
                }
                totalImGuiApplications += applied;
                Logger.LogInfo("IMGUI_SKIN_PATCHED styles=" + applied);
            }
            catch (Exception)
            {
                // OnGUI 内访问不应失败；失败则下帧重试
            }
        }

        // ── Harmony 补丁（Rendezvous 实证 2026-08-17）──
        // 对象扫描找不到场景 TMP 文本（FindObjectsOfTypeAll 返回 0）——
        // 改为 hook 文本设置入口：TMP_Text.set_text / 游戏
        // LocalizationText.ChangeLanguage，文本每次更新后强制字体替换。
        private void SetupHarmonyPatches()
        {
            // Rendezvous 实证 2026-08-17：插件 Awake 时游戏主程序集
            // （Assembly-CSharp）尚未加载 → LocalizationText 找不到 →
            // change_language 补丁缺失（唯一直接命中游戏文本设置的
            // 补丁）。Update 轮询里延迟重试（游戏程序集加载后命中）。
            try
            {
                Type tmpTextType = FindOptionalType("TMPro.TMP_Text");
                if (tmpTextType != null)
                {
                    _tmpSetTextMethod = tmpTextType.GetMethod(
                        "set_text",
                        BindingFlags.Public | BindingFlags.Instance);
                    _tmpOnEnableMethod = tmpTextType.GetMethod(
                        "OnEnable",
                        BindingFlags.Public | BindingFlags.NonPublic
                        | BindingFlags.Instance);
                    _tmpGenerateTextMeshMethod = tmpTextType.GetMethod(
                        "GenerateTextMesh",
                        BindingFlags.Public | BindingFlags.NonPublic
                        | BindingFlags.Instance);
                }
                Type uiTextTypeLocal = FindOptionalType(
                    "UnityEngine.UI.Text");
                Type locType = FindOptionalType("LocalizationText");
                if (locType != null)
                {
                    _locChangeLanguageMethod = locType.GetMethod(
                        "ChangeLanguage",
                        BindingFlags.Public | BindingFlags.Instance);
                }
                var harmony = new Harmony("com.hanhua.fontfallback");
                if (_tmpSetTextMethod != null)
                {
                    harmony.Patch(
                        _tmpSetTextMethod,
                        postfix: new HarmonyMethod(
                            typeof(HanhuaFontPlugin),
                            nameof(TmpSetTextPostfix)));
                }
                if (uiTextTypeLocal != null)
                {
                    MethodInfo uiSetText = uiTextTypeLocal.GetMethod(
                        "set_text",
                        BindingFlags.Public | BindingFlags.Instance);
                    if (uiSetText != null)
                    {
                        harmony.Patch(
                            uiSetText,
                            postfix: new HarmonyMethod(
                                typeof(HanhuaFontPlugin),
                                nameof(UiSetTextPostfix)));
                    }
                }
                if (_tmpOnEnableMethod != null)
                {
                    harmony.Patch(
                        _tmpOnEnableMethod,
                        postfix: new HarmonyMethod(
                            typeof(HanhuaFontPlugin),
                            nameof(TmpOnEnablePostfix)));
                }
                if (_tmpGenerateTextMeshMethod != null)
                {
                    harmony.Patch(
                        _tmpGenerateTextMeshMethod,
                        prefix: new HarmonyMethod(
                            typeof(HanhuaFontPlugin),
                            nameof(TmpGenerateTextMeshPrefix)));
                }
                if (_locChangeLanguageMethod != null)
                {
                    harmony.Patch(
                        _locChangeLanguageMethod,
                        postfix: new HarmonyMethod(
                            typeof(HanhuaFontPlugin),
                            nameof(ChangeLanguagePostfix)));
                }
                // 全部目标打上才算完成；change_language 缺失时 Update
                // 轮询重试（游戏主程序集加载后 Assembly-CSharp 可达）
                _harmonyPatched = (_tmpSetTextMethod != null
                    && _tmpOnEnableMethod != null
                    && _tmpGenerateTextMeshMethod != null
                    && uiTextTypeLocal != null
                    && _locChangeLanguageMethod != null);
                Logger.LogInfo(
                    "HARMONY_PATCHED tmp_text="
                    + (_tmpSetTextMethod != null)
                    + " tmp_enable=" + (_tmpOnEnableMethod != null)
                    + " tmp_mesh="
                    + (_tmpGenerateTextMeshMethod != null)
                    + " ui_text=" + (uiTextTypeLocal != null)
                    + " change_language="
                    + (_locChangeLanguageMethod != null)
                    + " complete=" + _harmonyPatched);
            }
            catch (Exception exception)
            {
                Logger.LogError(
                    "Harmony patch setup failed; font scans continue: "
                    + exception);
            }
        }

        // UnityEngine.UI.Text.set_text 后置：UGUI 文本更新后强制字体=
        // 工具字体（Rendezvous 实证：LocalizationText.t_text 是 UGUI
        // Text，官方子集字体缺字 → 口口；ChangeLanguage 补丁可能缺失，
        // set_text 补丁兜底覆盖一切 UGUI 文本）。
        private static int _harmonyLogCount;

        public static void UiSetTextPostfix(object __instance, string value)
        {
            HanhuaFontPlugin plugin = Instance;
            if (plugin == null || plugin.dynamicFont == null
                || __instance == null)
            {
                return;
            }
            try
            {
                Type type = __instance.GetType();
                PropertyInfo fontProperty = type.GetProperty(
                    "font", BindingFlags.Public | BindingFlags.Instance);
                if (fontProperty == null || !fontProperty.CanWrite)
                {
                    return;
                }
                object current = fontProperty.GetValue(__instance, null);
                if (current == null
                    || !ReferenceEquals(current, plugin.dynamicFont))
                {
                    fontProperty.SetValue(__instance, plugin.dynamicFont, null);
                    if (_harmonyLogCount < 30)
                    {
                        _harmonyLogCount++;
                        string preview = value == null ? "" :
                            (value.Length > 30 ? value.Substring(0, 30)
                             : value);
                        plugin.Logger.LogInfo(
                            "HARMONY_UI text=" + preview
                            + " oldFont=" + (current == null
                                ? "null" : current.ToString())
                            + " -> tool");
                    }
                }
            }
            catch (Exception)
            {
            }
        }

        private static bool IsUnityObjectUnavailable(object value)
        {
            if (ReferenceEquals(value, null))
            {
                return true;
            }
            UnityEngine.Object unityObject = value as UnityEngine.Object;
            return !ReferenceEquals(unityObject, null) && unityObject == null;
        }

        private static bool ApplyTmpFontToText(object target)
        {
            HanhuaFontPlugin plugin = Instance;
            if (plugin == null || plugin.dynamicTmpFont == null
                || IsUnityObjectUnavailable(target))
            {
                return false;
            }

            PropertyInfo fontProperty = target.GetType().GetProperty(
                "font", BindingFlags.Public | BindingFlags.Instance);
            if (fontProperty == null || !fontProperty.CanRead
                || !fontProperty.CanWrite
                || !fontProperty.PropertyType.IsInstanceOfType(
                    plugin.dynamicTmpFont))
            {
                return false;
            }

            object original = fontProperty.GetValue(target, null);
            if (ReferenceEquals(original, plugin.dynamicTmpFont))
            {
                return false;
            }
            if (!IsUnityObjectUnavailable(original)
                && plugin.tmpFontAssetType != null
                && plugin.tmpFontAssetType.IsInstanceOfType(original))
            {
                // Fallback wiring is best-effort.  The static tool font must
                // still become the main font even if TMP rejects a mutation.
                try
                {
                    plugin.AttachOriginalTmpFallback(original);
                }
                catch (Exception exception)
                {
                    plugin.Logger.LogWarning(
                        "Could not preserve TMP original fallback: "
                        + exception.Message);
                }
            }

            fontProperty.SetValue(target, plugin.dynamicTmpFont, null);
            return true;
        }

        private static string DescribeCurrentTmpFont(object target)
        {
            if (IsUnityObjectUnavailable(target))
            {
                return "null";
            }
            PropertyInfo fontProperty = target.GetType().GetProperty(
                "font", BindingFlags.Public | BindingFlags.Instance);
            if (fontProperty == null || !fontProperty.CanRead)
            {
                return "unavailable";
            }
            object current = fontProperty.GetValue(target, null);
            return IsUnityObjectUnavailable(current)
                ? "null" : current.ToString();
        }

        private void AttachOriginalTmpFallback(object original)
        {
            if (dynamicTmpFont == null || IsUnityObjectUnavailable(original)
                || ReferenceEquals(original, dynamicTmpFont)
                || tmpFontAssetType == null
                || !tmpFontAssetType.IsInstanceOfType(original))
            {
                return;
            }

            PropertyInfo originalFallbackProperty = original.GetType().GetProperty(
                "fallbackFontAssetTable",
                BindingFlags.Public | BindingFlags.Instance);
            if (originalFallbackProperty != null
                && originalFallbackProperty.CanRead)
            {
                IList originalFallbacks = originalFallbackProperty.GetValue(
                    original, null) as IList;
                if (originalFallbacks != null
                    && originalFallbacks.Contains(dynamicTmpFont))
                {
                    return;
                }
            }

            PropertyInfo fallbackProperty = dynamicTmpFont.GetType().GetProperty(
                "fallbackFontAssetTable",
                BindingFlags.Public | BindingFlags.Instance);
            if (fallbackProperty == null || !fallbackProperty.CanRead)
            {
                return;
            }

            IList fallbacks = fallbackProperty.GetValue(
                dynamicTmpFont, null) as IList;
            if (fallbacks == null)
            {
                if (!fallbackProperty.CanWrite)
                {
                    return;
                }
                Type listType = typeof(List<>).MakeGenericType(tmpFontAssetType);
                fallbacks = Activator.CreateInstance(listType) as IList;
                if (fallbacks == null)
                {
                    return;
                }
                fallbackProperty.SetValue(dynamicTmpFont, fallbacks, null);
            }

            if (fallbacks.Contains(original))
            {
                return;
            }
            fallbacks.Add(original);
        }

        // TMP_Text.set_text 后置：文本更新后强制主字体=工具字体
        // （TMP 2.x 无字体时用内置默认/官方子集字体 → 缺字口口。
        // 不依赖对象扫描——每个文本每次更新都执行）。
        public static void TmpSetTextPostfix(object __instance, string value)
        {
            HanhuaFontPlugin plugin = Instance;
            if (plugin == null || plugin.dynamicTmpFont == null
                || __instance == null)
            {
                return;
            }
            try
            {
                string oldFont = DescribeCurrentTmpFont(__instance);
                if (ApplyTmpFontToText(__instance))
                {
                    if (_harmonyLogCount < 30)
                    {
                        _harmonyLogCount++;
                        string preview = value == null ? "" :
                            (value.Length > 30 ? value.Substring(0, 30)
                             : value);
                        plugin.Logger.LogInfo(
                            "HARMONY_TMP text=" + preview
                            + " oldFont=" + oldFont
                            + " -> tool");
                    }
                }
            }
            catch (Exception)
            {
            }
        }

        // TMP objects may receive their final text through a wrapper that
        // never calls TMP_Text.set_text after the component is enabled.  The
        // lifecycle hook catches that path before the first render.
        public static void TmpOnEnablePostfix(object __instance)
        {
            HanhuaFontPlugin plugin = Instance;
            if (plugin == null || plugin.dynamicTmpFont == null
                || __instance == null)
            {
                return;
            }
            try
            {
                ApplyTmpFontToText(__instance);
            }
            catch (Exception exception)
            {
                plugin.Logger.LogWarning(
                    "Could not apply TMP font on enable: "
                    + exception.Message);
            }
        }

        // Final common TMP render path; catches custom wrappers that never
        // call the public text setter after assigning translated text.
        public static void TmpGenerateTextMeshPrefix(object __instance)
        {
            HanhuaFontPlugin plugin = Instance;
            if (plugin == null || plugin.dynamicTmpFont == null
                || __instance == null)
            {
                return;
            }
            try
            {
                ApplyTmpFontToText(__instance);
            }
            catch (Exception exception)
            {
                plugin.Logger.LogWarning(
                    "Could not apply TMP font before mesh generation: "
                    + exception.Message);
            }
        }

        // LocalizationText.ChangeLanguage 后置：游戏侧文本设置完成后
        // 统一把 t_text（UGUI）与 t_textTMP（TMP）字体替换为工具字体。
        public static void ChangeLanguagePostfix(object __instance)
        {
            HanhuaFontPlugin plugin = Instance;
            if (plugin == null || __instance == null
                || plugin.dynamicFont == null)
            {
                return;
            }
            try
            {
                Type type = __instance.GetType();
                FieldInfo uguiField = type.GetField(
                    "t_text", BindingFlags.Public | BindingFlags.NonPublic
                    | BindingFlags.Instance);
                FieldInfo tmpField = type.GetField(
                    "t_textTMP", BindingFlags.Public | BindingFlags.NonPublic
                    | BindingFlags.Instance);
                if (uguiField != null)
                {
                    object ugui = uguiField.GetValue(__instance);
                    if (ugui != null)
                    {
                        PropertyInfo fontProp = ugui.GetType().GetProperty(
                            "font",
                            BindingFlags.Public | BindingFlags.Instance);
                        if (fontProp != null && fontProp.CanWrite)
                        {
                            fontProp.SetValue(ugui, plugin.dynamicFont, null);
                        }
                    }
                }
                if (tmpField != null && plugin.dynamicTmpFont != null)
                {
                    object tmp = tmpField.GetValue(__instance);
                    ApplyTmpFontToText(tmp);
                }
            }
            catch (Exception)
            {
            }
        }

        private void OnDestroy()
        {
            SceneManager.sceneLoaded -= OnSceneLoaded;
            translationApplicationStates.Clear();
            try
            {
                RemoveDynamicTmpFallbacks();
            }
            catch (Exception exception)
            {
                Logger.LogWarning("TMP fallback cleanup failed: " + exception);
            }

            try
            {
                bool bundleOwned = tmpFontSource == "bundle";
                if (dynamicTmpFont != null)
                {
                    if (!bundleOwned)
                    {
                        Destroy(dynamicTmpFont);
                    }
                    dynamicTmpFont = null;
                }

                if (tmpFontBundle != null)
                {
                    tmpFontBundle.Unload(false);
                    tmpFontBundle = null;
                }
                tmpFontSource = "";

                CleanupTmpFontCandidates(tmpFontCandidates, dynamicFont);
                tmpFontCandidates.Clear();
                tmpSourceFont = null;

                if (dynamicFont != null)
                {
                    Destroy(dynamicFont);
                    dynamicFont = null;
                }
                tmpSourceFont = null;
            }
            catch (Exception exception)
            {
                Logger.LogWarning("Dynamic font object cleanup failed: " + exception);
            }

            if (!string.IsNullOrEmpty(fontPath))
            {
                try
                {
                    RemoveFontResourceEx(fontPath, FrPrivate, IntPtr.Zero);
                }
                catch (Exception exception)
                {
                    Logger.LogWarning("Private font cleanup failed: " + exception);
                }
            }
        }

        private void LoadFontPayload()
        {
            fontPath = Path.GetFullPath(Path.Combine(pluginDirectory, "font.ttf"));
            string familyPath = Path.GetFullPath(
                Path.Combine(pluginDirectory, "font-family.txt"));

            if (!File.Exists(fontPath))
            {
                throw new FileNotFoundException("Font payload is missing", fontPath);
            }

            if (!File.Exists(familyPath))
            {
                throw new FileNotFoundException("Font family metadata is missing", familyPath);
            }

            string family = File.ReadAllText(
                familyPath,
                new UTF8Encoding(false, true)).Trim();
            if (family.Length == 0)
            {
                throw new FormatException("font-family.txt is empty");
            }

            int installedFonts = AddFontResourceEx(fontPath, FrPrivate, IntPtr.Zero);
            if (installedFonts == 0)
            {
                Logger.LogWarning(
                    "AddFontResourceEx returned 0; attempting Unity font creation anyway. Win32="
                    + Marshal.GetLastWin32Error());
            }

            try
            {
                InitializeLegacyFont(family);
                legacyStatus = "ready";
            }
            catch (Exception exception)
            {
                legacyStatus = "failed";
                legacyError = exception.GetType().Name + ": " + exception.Message;
                Logger.LogError("Legacy/UI/TextMesh font initialization failed: " + exception);
            }

            InitializeUiToolkitAdapter();

            try
            {
                InitializeTmpFont(family);
                tmpStatus = "ready";
            }
            catch (Exception exception)
            {
                tmpStatus = "failed";
                tmpError = exception.GetType().Name + ": " + exception.Message;
                Logger.LogError("TMP font initialization failed; legacy adapters continue: " + exception);
            }

            Logger.LogInfo(
                "FONT_FAMILY_LOADED family=" + family + " private_fonts=" + installedFonts);
        }

        private void InitializeLegacyFont(string family)
        {
            dynamicFont = Font.CreateDynamicFontFromOSFont(family, 32);
            if (dynamicFont == null)
            {
                throw new InvalidOperationException(
                    "Font.CreateDynamicFontFromOSFont returned null for " + family);
            }

            dynamicFont.name = "Hanhua Dynamic Font (" + family + ")";
        }

        private static Type FindOptionalType(string fullName)
        {
            foreach (Assembly assembly in AppDomain.CurrentDomain.GetAssemblies())
            {
                Type found = assembly.GetType(fullName, false);
                if (found != null)
                {
                    return found;
                }
            }
            // Rendezvous 实证 2026-08-17：UnityEngine.UI 等按需程序集在
            // 相关场景加载前未入 AppDomain → GetAssemblies 遍历不到 →
            // uiTextType 缓存 null → UGUI 文本永远未 patch（静态写回
            // 中文用游戏原字体渲染 → 口口口，ui 恒 0）。按类型名的
            // 程序集部分显式加载（BepInEx 环境按名解析）。
            // TMP 陷阱：命名空间是 TMPro 但程序集是 Unity.TextMeshPro
            // （TMP 2.x/3.x）——Assembly.Load("TMPro") 必失败 → 类型
            // 永远 null。候选程序集逐个尝试。
            int lastDot = fullName.LastIndexOf('.');
            string[] candidates;
            if (lastDot <= 0)
            {
                // Rendezvous 实证 2026-08-17：LocalizationText 等在全局
                // 命名空间（无点号）——必须尝试游戏主程序集，否则
                // Harmony 补丁打不上、UGUI 文本字体永远不替换。
                candidates = new[] {
                    "Assembly-CSharp",
                    "Assembly-CSharp-firstpass",
                };
            }
            else
            {
                string namespacePart = fullName.Substring(0, lastDot);
                candidates = new[] {
                    namespacePart,
                    // TMP 2.x/3.x（Unity 2019+，TextMeshPro 2.x 程序集名）
                    "Unity.TextMeshPro",
                    // TMP 1.x / 老工程
                    "TextMeshPro",
                };
            }
            foreach (string assemblyName in candidates)
            {
                try
                {
                    Assembly loaded = Assembly.Load(assemblyName);
                    Type found = loaded.GetType(fullName, false);
                    if (found != null)
                    {
                        return found;
                    }
                }
                catch (Exception)
                {
                    // 该候选程序集不存在 → 尝试下一个
                }
            }
            return null;
        }

        private static UnityEngine.Object[] FindObjectsOfOptionalType(Type type)
        {
            return type == null
                ? new UnityEngine.Object[0]
                : Resources.FindObjectsOfTypeAll(type);
        }

        private void DiscoverOptionalTextTypes()
        {
            tmpFontAssetType = FindOptionalType("TMPro.TMP_FontAsset");
            tmpSettingsType = FindOptionalType("TMPro.TMP_Settings");
            tmpTextType = FindOptionalType("TMPro.TMP_Text");
            uiTextType = FindOptionalType("UnityEngine.UI.Text");
            uiToolkitTextElementType = FindOptionalType(
                "UnityEngine.UIElements.TextElement");
            uiToolkitDocumentType = FindOptionalType(
                "UnityEngine.UIElements.UIDocument");
        }

        private void InitializeUiToolkitAdapter()
        {
            if (uiToolkitTextElementType == null || uiToolkitDocumentType == null)
            {
                uiToolkitStatus = "unsupported";
                uiToolkitError = "UI Toolkit TextElement or UIDocument is unavailable";
                Logger.LogInfo("UITOOLKIT_ADAPTER_UNSUPPORTED reason=" + uiToolkitError);
                return;
            }
            uiToolkitStatus = dynamicFont == null ? "failed" : "available";
            uiToolkitError = dynamicFont == null
                ? "Legacy dynamic font is unavailable"
                : "";
        }

        private bool TryLoadBundledTmpFont()
        {
            string bundlePath = Path.Combine(pluginDirectory, "font-tmp.bundle");
            if (!File.Exists(bundlePath))
            {
                Logger.LogInfo(
                    "TMP_BUNDLE_FALLBACK reason=missing path=" + bundlePath);
                return false;
            }

            AssetBundle loadedBundle = null;
            try
            {
                loadedBundle = AssetBundle.LoadFromFile(bundlePath);
                if (loadedBundle == null)
                {
                    throw new InvalidOperationException(
                        "AssetBundle.LoadFromFile returned null");
                }

                UnityEngine.Object[] assets =
                    loadedBundle.LoadAllAssets(tmpFontAssetType);
                List<UnityEngine.Object> fonts = new List<UnityEngine.Object>();
                foreach (UnityEngine.Object asset in assets)
                {
                    if (asset != null && tmpFontAssetType.IsInstanceOfType(asset))
                    {
                        fonts.Add(asset);
                    }
                }
                if (fonts.Count != 1)
                {
                    throw new InvalidOperationException(
                        "Expected exactly one TMP_FontAsset, got " + fonts.Count);
                }

                tmpFontBundle = loadedBundle;
                dynamicTmpFont = fonts[0];
                tmpFontSource = "bundle";
                activeTmpFactoryArgument = bundlePath;
                tmpFactoryVerified = false;
                EnsureGlobalTmpFallback();
                Logger.LogInfo(
                    "TMP_BUNDLE_READY path=" + bundlePath
                    + " asset=" + dynamicTmpFont.name);
                return true;
            }
            catch (Exception exception)
            {
                if (loadedBundle != null)
                {
                    loadedBundle.Unload(false);
                }
                tmpFontBundle = null;
                dynamicTmpFont = null;
                tmpFontSource = "";
                activeTmpFactoryArgument = null;
                tmpFactoryVerified = false;
                Logger.LogWarning(
                    "TMP_BUNDLE_FALLBACK reason=" + exception.GetType().Name
                    + ": " + exception.Message);
                return false;
            }
        }

        private void InitializeTmpFont(string family)
        {
            tmpFontFamily = family;
            InitializeTmpFont(family, true);
        }

        private void InitializeTmpFont(string family, bool tryBundledFont)
        {
            if (tmpFontAssetType == null)
            {
                throw new NotSupportedException(
                    "TMPro.TMP_FontAsset is unavailable; TMP adapter is optional");
            }
            if (tryBundledFont && TryLoadBundledTmpFont())
            {
                return;
            }

            List<MethodInfo> fileFactories = new List<MethodInfo>();
            List<MethodInfo> fontFactories = new List<MethodInfo>();
            foreach (MethodInfo method in tmpFontAssetType.GetMethods(
                BindingFlags.Public | BindingFlags.Static))
            {
                if (!string.Equals(method.Name, "CreateFontAsset", StringComparison.Ordinal))
                {
                    continue;
                }

                ParameterInfo[] parameters = method.GetParameters();
                if (parameters.Length == 0)
                {
                    continue;
                }

                if (parameters[0].ParameterType == typeof(string))
                {
                    fileFactories.Add(method);
                }
                else if (parameters[0].ParameterType == typeof(Font))
                {
                    fontFactories.Add(method);
                }
            }

            List<MethodInfo> factories = new List<MethodInfo>();
            SortTmpFactoriesBySpecificity(fileFactories);
            SortTmpFactoriesBySpecificity(fontFactories);
            factories.AddRange(fileFactories);
            factories.AddRange(fontFactories);
            if (factories.Count == 0)
            {
                throw new MissingMethodException(
                    "No compatible TMP_FontAsset CreateFontAsset overload was found");
            }

            tmpFactoryAttempts.Clear();
            CleanupTmpFontCandidates(tmpFontCandidates, dynamicFont);
            tmpFontCandidates.Clear();
            tmpFontCandidates.AddRange(BuildTmpFontCandidates(family));
            nextTmpFactoryAttempt = 0;
            tmpFactoryVerified = false;
            List<string> factoryFailures = new List<string>();
            foreach (MethodInfo selected in factories)
            {
                bool usesFilePath = selected.GetParameters()[0].ParameterType
                    == typeof(string);
                List<object> firstArguments = new List<object>();
                if (usesFilePath)
                {
                    firstArguments.Add(fontPath);
                }
                else
                {
                    foreach (Font candidate in tmpFontCandidates)
                    {
                        firstArguments.Add(candidate);
                    }
                }

                foreach (object firstArgument in firstArguments)
                {
                    tmpFactoryAttempts.Add(new TmpFactoryAttempt
                    {
                        Factory = selected,
                        FirstArgument = firstArgument,
                    });
                }
            }

            if (!ActivateNextTmpFont(factoryFailures))
            {
                CleanupTmpFontCandidates(tmpFontCandidates, dynamicFont);
                tmpFontCandidates.Clear();
                throw new InvalidOperationException(
                    "All compatible TMP_FontAsset CreateFontAsset overloads failed: "
                    + string.Join(" | ", factoryFailures.ToArray()));
            }
        }

        private bool ActivateNextTmpFont(List<string> factoryFailures)
        {
            while (nextTmpFactoryAttempt < tmpFactoryAttempts.Count)
            {
                TmpFactoryAttempt attempt =
                    tmpFactoryAttempts[nextTmpFactoryAttempt++];
                try
                {
                    object[] arguments = BuildTmpFactoryArguments(
                        attempt.Factory, attempt.FirstArgument);
                    dynamicTmpFont = attempt.Factory.Invoke(
                        null, arguments) as UnityEngine.Object;
                    if (dynamicTmpFont == null)
                    {
                        throw new InvalidOperationException(
                            "TMP dynamic fallback creation returned null");
                    }

                    dynamicTmpFont.name = "Hanhua TMP Dynamic Fallback";
                    tmpFontSource = "dynamic";
                    SetOptionalProperty(
                        dynamicTmpFont, "atlasPopulationMode", "Dynamic");
                    SetOptionalProperty(
                        dynamicTmpFont, "isMultiAtlasTexturesEnabled", true);
                    tmpSourceFont = attempt.FirstArgument as Font;
                    activeTmpFactoryArgument = attempt.FirstArgument;
                    tmpFactoryVerified = false;
                    EnsureGlobalTmpFallback();
                    Logger.LogInfo(
                        "TMP_FACTORY_CREATED factory=" + attempt.Factory
                        + " candidate=" + DescribeTmpCandidate(
                            attempt.FirstArgument));
                    return true;
                }
                catch (Exception exception)
                {
                    factoryFailures.Add(DescribeFactoryFailure(
                        attempt.Factory, attempt.FirstArgument, exception));
                    if (dynamicTmpFont != null)
                    {
                        Destroy(dynamicTmpFont);
                        dynamicTmpFont = null;
                    }
                    tmpFontSource = "";
                    tmpSourceFont = null;
                    activeTmpFactoryArgument = null;
                }
            }
            return false;
        }

        private static string DescribeTmpCandidate(object candidate)
        {
            return candidate is Font
                ? ((Font)candidate).name
                : Convert.ToString(candidate);
        }

        private List<Font> BuildTmpFontCandidates(string family)
        {
            List<Font> candidates = new List<Font>();
            if (dynamicFont != null)
            {
                candidates.Add(dynamicFont);
            }
            try
            {
                Font familyFont = new Font(family);
                familyFont.name = "Hanhua TMP Source (family)";
                candidates.Add(familyFont);
            }
            catch (Exception exception)
            {
                Logger.LogWarning("TMP family Font candidate failed: " + exception);
            }
            try
            {
                Font pathFont = new Font(fontPath);
                pathFont.name = "Hanhua TMP Source (path)";
                candidates.Add(pathFont);
            }
            catch (Exception exception)
            {
                Logger.LogWarning("TMP path Font candidate failed: " + exception);
            }
            return candidates;
        }

        private void CleanupTmpFontCandidates(List<Font> candidates, Font keep)
        {
            foreach (Font candidate in candidates)
            {
                if (candidate != null
                    && !ReferenceEquals(candidate, keep)
                    && !ReferenceEquals(candidate, dynamicFont))
                {
                    Destroy(candidate);
                }
            }
        }

        private static string DescribeFactoryFailure(
            MethodInfo factory,
            object firstArgument,
            Exception exception)
        {
            Exception root = exception;
            while (root is TargetInvocationException && root.InnerException != null)
            {
                root = root.InnerException;
            }
            return factory + " [" + DescribeTmpCandidate(firstArgument) + "] => "
                + root.GetType().Name + ": " + root.Message;
        }

        private static void SortTmpFactoriesBySpecificity(
            List<MethodInfo> factories)
        {
            factories.Sort(delegate(MethodInfo left, MethodInfo right)
            {
                return right.GetParameters().Length.CompareTo(
                    left.GetParameters().Length);
            });
        }

        private object[] BuildTmpFactoryArguments(
            MethodInfo method,
            object firstArgument)
        {
            ParameterInfo[] parameters = method.GetParameters();
            object[] arguments = new object[parameters.Length];
            arguments[0] = firstArgument;
            for (int index = 1; index < parameters.Length; index++)
            {
                ParameterInfo parameter = parameters[index];
                Type type = parameter.ParameterType;
                string name = parameter.Name ?? "";
                if (parameter.IsOptional)
                {
                    // mscorlib 2.0（CLR 2.0，Unity 2018.2 及更早）没有
                    // ParameterInfo.HasDefaultValue（.NET 4.5+）；IsOptional
                    // 两代 CLR 都有（可选参数编译为 [Optional]），构建保持
                    // CLR 2.0 兼容。
                    arguments[index] = parameter.DefaultValue;
                }
                else if (type.IsEnum)
                {
                    try
                    {
                        arguments[index] = name.IndexOf(
                            "population", StringComparison.OrdinalIgnoreCase) >= 0
                            ? Enum.Parse(type, "Dynamic", true)
                            : Enum.Parse(type, "SDFAA", true);
                    }
                    catch (ArgumentException)
                    {
                        arguments[index] = Activator.CreateInstance(type);
                    }
                }
                else if (type == typeof(int))
                {
                    // Rendezvous 实证 2026-08-17：atlas 2048 + sampleSize 32
                    // + padding 9 → 每字形 50px² → 容量 ≈1677，required 集
                    // 1769 码点溢出 → TMP 文本口口口（格式无关——TTF/OTF
                    // 同样缺）。atlas 4096（TMP 上限）→ 容量 4 倍 → 覆盖
                    // 需求集绰绰有余。
                    arguments[index] = name.IndexOf(
                        "padding", StringComparison.OrdinalIgnoreCase) >= 0
                        ? 9
                        : name.IndexOf("atlas", StringComparison.OrdinalIgnoreCase) >= 0
                            ? 4096
                            : name.IndexOf("face", StringComparison.OrdinalIgnoreCase) >= 0
                                ? 0
                                : 32;
                }
                else if (type == typeof(bool))
                {
                    arguments[index] = true;
                }
                else if (type == typeof(Shader))
                {
                    // CreateFontAsset 带 shader 参数的重载（TMP 3.x 完整
                    // 版）。此前落入 Activator.CreateInstance → Shader 无
                    // 公共无参构造 → 重载必然失败；且 TMP 1 参数版内部
                    // 从 TMP_Settings 拿 shader，游戏无默认字体时为 null
                    // → 动态字体创建失败 → 中文文本无字形消失
                    // （deepest-sword 实证）。显式解析 SDF shader。
                    arguments[index] = ResolveTmpSdfShader();
                }
                else
                {
                    arguments[index] = Activator.CreateInstance(type);
                }
            }

            return arguments;
        }

        private Shader ResolveTmpSdfShader()
        {
            // TMP 动态字体创建需要 SDF shader。查找顺序：显式 Shader.Find
            // （TMP 3.x 经典路径）→ TMP_Settings 默认字体的材质 shader →
            // 已加载 TMP 材质的 shader 借用。全部失败返回 null——TMP
            // CreateFontAsset 抛 shader null 异常（deepest-sword 实证：
            // 游戏无 TMP 默认字体时 CreateFontAsset(Font) 内部 shader 为
            // null → 动态字体创建失败 → TMP 适配器 failed → 场景中文
            // 文本无字形显示为空/消失）。
            Shader shader = Shader.Find("TextMeshPro/Distance Field");
            if (shader != null)
            {
                return shader;
            }
            shader = Shader.Find("TextMeshPro/Mobile/Distance Field");
            if (shader != null)
            {
                return shader;
            }
            try
            {
                if (tmpSettingsType != null)
                {
                    PropertyInfo property = tmpSettingsType.GetProperty(
                        "defaultFontAsset",
                        BindingFlags.Public | BindingFlags.Static);
                    if (property != null)
                    {
                        object defaultFontAsset = property.GetValue(null, null);
                        if (defaultFontAsset != null)
                        {
                            PropertyInfo materialProperty = defaultFontAsset
                                .GetType().GetProperty(
                                    "material",
                                    BindingFlags.Public | BindingFlags.Instance);
                            if (materialProperty != null)
                            {
                                Material material = materialProperty.GetValue(
                                    defaultFontAsset, null) as Material;
                                if (material != null && material.shader != null)
                                {
                                    return material.shader;
                                }
                            }
                        }
                    }
                }
            }
            catch (Exception exception)
            {
                Logger.LogWarning(
                    "TMP default font shader lookup failed: " + exception);
            }
            try
            {
                foreach (Material material in Resources.FindObjectsOfTypeAll<Material>())
                {
                    if (material != null && material.shader != null
                        && material.shader.name.IndexOf(
                            "TextMeshPro", StringComparison.OrdinalIgnoreCase) >= 0)
                    {
                        return material.shader;
                    }
                }
            }
            catch (Exception exception)
            {
                Logger.LogWarning(
                    "TMP material shader scan failed: " + exception);
            }
            return null;
        }

        private static void SetOptionalProperty(object target, string name, object value)
        {
            PropertyInfo property = target.GetType().GetProperty(
                name,
                BindingFlags.Public | BindingFlags.Instance);
            if (property == null || !property.CanWrite)
            {
                return;
            }

            object converted = value;
            if (property.PropertyType.IsEnum && value is string)
            {
                converted = Enum.Parse(property.PropertyType, (string)value, true);
            }
            property.SetValue(target, converted, null);
        }

        private void LoadExactTranslations()
        {
            string path = Path.Combine(pluginDirectory, "translations.json");
            exactTranslations.Clear();
            normalizedTranslations.Clear();
            conflictingNormalizedTranslations.Clear();
            runtimeTemplates.Clear();
            LoadExclusionTable();
            if (!File.Exists(path))
            {
                Logger.LogInfo("EXACT_TRANSLATIONS_READY count=0 file=missing");
                return;
            }

            string payload = File.ReadAllText(path, new UTF8Encoding(false, true));
            foreach (KeyValuePair<string, string> pair in ParseStringMap(payload))
            {
                if (pair.Key.Length > 0 && pair.Value.Length > 0)
                {
                    exactTranslations[pair.Key] = pair.Value;
                }
            }
            BuildNormalizedTranslations();
            LoadRuntimeTemplates();
            Logger.LogInfo("EXACT_TRANSLATIONS_READY count=" + exactTranslations.Count);
        }

        private void LoadExclusionTable()
        {
            excludedTranslations.Clear();
            excludedNormalizedTranslations.Clear();
            string path = Path.Combine(pluginDirectory, "translations-exclude.json");
            if (!File.Exists(path))
            {
                Logger.LogInfo("TRANSLATIONS_EXCLUDED count=0 file=missing");
                return;
            }
            string payload = File.ReadAllText(path, new UTF8Encoding(false, true));
            int cursor = 0;
            List<string> excluded = ReadJsonStringArray(payload, ref cursor);
            foreach (string value in excluded)
            {
                if (value.Length == 0)
                {
                    continue;
                }
                excludedTranslations.Add(value);
                string normalized = NormalizeRuntimeText(value);
                if (normalized.Length > 0)
                {
                    excludedNormalizedTranslations.Add(normalized);
                }
            }
            Logger.LogInfo("TRANSLATIONS_EXCLUDED count=" + excludedTranslations.Count);
        }

        private void BuildNormalizedTranslations()
        {
            foreach (KeyValuePair<string, string> pair in exactTranslations)
            {
                string normalized = NormalizeRuntimeText(pair.Key);
                string existing;
                if (normalized.Length == 0
                    || conflictingNormalizedTranslations.Contains(normalized))
                {
                    continue;
                }
                if (normalizedTranslations.TryGetValue(normalized, out existing)
                    && !string.Equals(existing, pair.Value, StringComparison.Ordinal))
                {
                    normalizedTranslations.Remove(normalized);
                    conflictingNormalizedTranslations.Add(normalized);
                    continue;
                }
                normalizedTranslations[normalized] = pair.Value;
            }
        }

        private static string NormalizeRuntimeText(string value)
        {
            return (value ?? "").Replace("\r\n", "\n").Replace("\r", "\n").Trim();
        }

        private void LoadRuntimeTemplates()
        {
            string path = Path.Combine(pluginDirectory, "runtime-templates.json");
            if (!File.Exists(path))
            {
                Logger.LogInfo("RUNTIME_TEMPLATES_READY count=0 file=missing");
                return;
            }
            string payload = File.ReadAllText(path, new UTF8Encoding(false, true));
            runtimeTemplates.AddRange(ParseRuntimeTemplates(payload));
            Logger.LogInfo("RUNTIME_TEMPLATES_READY count=" + runtimeTemplates.Count);
        }

        private static List<RuntimeTranslationTemplate> ParseRuntimeTemplates(
            string payload)
        {
            int cursor = 0;
            List<RuntimeTranslationTemplate> parsed =
                new List<RuntimeTranslationTemplate>();
            ExpectJsonCharacter(payload, ref cursor, '{');
            ExpectJsonProperty(payload, ref cursor, "schema_version");
            SkipJsonWhitespace(payload, ref cursor);
            if (cursor >= payload.Length || payload[cursor] != '1')
            {
                throw new FormatException("runtime template schema mismatch");
            }
            cursor++;
            ExpectJsonCharacter(payload, ref cursor, ',');
            ExpectJsonProperty(payload, ref cursor, "templates");
            ExpectJsonCharacter(payload, ref cursor, '[');
            SkipJsonWhitespace(payload, ref cursor);
            while (cursor < payload.Length && payload[cursor] != ']')
            {
                ExpectJsonCharacter(payload, ref cursor, '{');
                RuntimeTranslationTemplate template =
                    new RuntimeTranslationTemplate();
                ExpectJsonProperty(payload, ref cursor, "slots");
                template.slots = ReadJsonStringArray(payload, ref cursor);
                ExpectJsonCharacter(payload, ref cursor, ',');
                ExpectJsonProperty(payload, ref cursor, "source_fragments");
                template.sourceFragments = ReadJsonStringArray(payload, ref cursor);
                ExpectJsonCharacter(payload, ref cursor, ',');
                ExpectJsonProperty(payload, ref cursor, "target_fragments");
                template.targetFragments = ReadJsonStringArray(payload, ref cursor);
                ExpectJsonCharacter(payload, ref cursor, '}');
                if (template.slots.Count == 0
                    || template.sourceFragments.Count != template.slots.Count + 1
                    || template.targetFragments.Count != template.slots.Count + 1)
                {
                    throw new FormatException("runtime template shape is invalid");
                }
                parsed.Add(template);
                SkipJsonWhitespace(payload, ref cursor);
                if (cursor < payload.Length && payload[cursor] == ',')
                {
                    cursor++;
                    SkipJsonWhitespace(payload, ref cursor);
                }
                else
                {
                    break;
                }
            }
            ExpectJsonCharacter(payload, ref cursor, ']');
            ExpectJsonCharacter(payload, ref cursor, '}');
            SkipJsonWhitespace(payload, ref cursor);
            if (cursor != payload.Length)
            {
                throw new FormatException("runtime template JSON has trailing data");
            }
            return parsed;
        }

        private static void SkipJsonWhitespace(string payload, ref int cursor)
        {
            while (cursor < payload.Length && char.IsWhiteSpace(payload[cursor]))
            {
                cursor++;
            }
        }

        private static void ExpectJsonCharacter(
            string payload, ref int cursor, char expected)
        {
            SkipJsonWhitespace(payload, ref cursor);
            if (cursor >= payload.Length || payload[cursor] != expected)
            {
                throw new FormatException(
                    "runtime template JSON expected " + expected);
            }
            cursor++;
        }

        private static void ExpectJsonProperty(
            string payload, ref int cursor, string expected)
        {
            string actual = ReadJsonStringValue(payload, ref cursor);
            if (!string.Equals(actual, expected, StringComparison.Ordinal))
            {
                throw new FormatException(
                    "runtime template JSON property mismatch");
            }
            ExpectJsonCharacter(payload, ref cursor, ':');
        }

        private static string ReadJsonStringValue(string payload, ref int cursor)
        {
            SkipJsonWhitespace(payload, ref cursor);
            if (cursor >= payload.Length || payload[cursor] != '"')
            {
                throw new FormatException("runtime template JSON string expected");
            }
            cursor++;
            StringBuilder encoded = new StringBuilder();
            while (cursor < payload.Length)
            {
                char current = payload[cursor++];
                if (current == '"')
                {
                    return DecodeJsonString(encoded.ToString());
                }
                encoded.Append(current);
                if (current == '\\')
                {
                    if (cursor >= payload.Length)
                    {
                        break;
                    }
                    encoded.Append(payload[cursor++]);
                }
            }
            throw new FormatException("unterminated runtime template string");
        }

        private static List<string> ReadJsonStringArray(
            string payload, ref int cursor)
        {
            List<string> values = new List<string>();
            ExpectJsonCharacter(payload, ref cursor, '[');
            SkipJsonWhitespace(payload, ref cursor);
            while (cursor < payload.Length && payload[cursor] != ']')
            {
                values.Add(ReadJsonStringValue(payload, ref cursor));
                SkipJsonWhitespace(payload, ref cursor);
                if (cursor < payload.Length && payload[cursor] == ',')
                {
                    cursor++;
                    SkipJsonWhitespace(payload, ref cursor);
                }
                else
                {
                    break;
                }
            }
            ExpectJsonCharacter(payload, ref cursor, ']');
            return values;
        }

        private static Dictionary<string, string> ParseStringMap(string payload)
        {
            string trimmed = (payload ?? "").Trim();
            if (trimmed.Length < 2 || trimmed[0] != '{'
                || trimmed[trimmed.Length - 1] != '}')
            {
                throw new FormatException("translations.json must be a JSON object");
            }

            Dictionary<string, string> parsed =
                new Dictionary<string, string>(StringComparer.Ordinal);
            MatchCollection matches = Regex.Matches(
                trimmed,
                "\\\"((?:\\\\.|[^\\\"\\\\])*)\\\"\\s*:\\s*"
                + "\\\"((?:\\\\.|[^\\\"\\\\])*)\\\"");
            foreach (Match match in matches)
            {
                parsed[DecodeJsonString(match.Groups[1].Value)] =
                    DecodeJsonString(match.Groups[2].Value);
            }
            StringBuilder residue = new StringBuilder();
            int cursor = 1;
            foreach (Match match in matches)
            {
                residue.Append(trimmed.Substring(cursor, match.Index - cursor));
                cursor = match.Index + match.Length;
            }
            residue.Append(trimmed.Substring(
                cursor,
                trimmed.Length - 1 - cursor));
            if (residue.ToString().Trim(' ', '\t', '\r', '\n', ',').Length != 0)
            {
                throw new FormatException(
                    "translations.json contains unsupported or malformed values");
            }
            if (trimmed != "{}" && matches.Count == 0)
            {
                throw new FormatException(
                    "translations.json contains no valid string mappings");
            }
            return parsed;
        }

        private static string DecodeJsonString(string value)
        {
            StringBuilder output = new StringBuilder(value.Length);
            for (int index = 0; index < value.Length; index++)
            {
                char current = value[index];
                if (current != '\\')
                {
                    output.Append(current);
                    continue;
                }
                if (++index >= value.Length)
                {
                    throw new FormatException("Invalid JSON escape");
                }
                char escaped = value[index];
                if (escaped == 'u')
                {
                    if (index + 4 >= value.Length)
                    {
                        throw new FormatException("Invalid JSON unicode escape");
                    }
                    output.Append((char)Convert.ToInt32(
                        value.Substring(index + 1, 4), 16));
                    index += 4;
                }
                else
                {
                    const string escapedChars = "\"\\/bfnrt";
                    const string decodedChars = "\"\\/\b\f\n\r\t";
                    int escapedIndex = escapedChars.IndexOf(escaped);
                    if (escapedIndex < 0)
                    {
                        throw new FormatException("Unknown JSON escape");
                    }
                    output.Append(decodedChars[escapedIndex]);
                }
            }
            return output.ToString();
        }

        private void WriteHealthManifest(bool requestDynamicAdd)
        {
            try
            {
                long lastSeenSeconds =
                    (long)(DateTime.UtcNow - new DateTime(1970, 1, 1, 0, 0, 0,
                        DateTimeKind.Utc)).TotalSeconds;
                char probe = RepresentativeGlyph();
                bool legacyGlyph = dynamicFont != null
                    && dynamicFont.HasCharacter(probe);
                bool tmpGlyph = requestDynamicAdd
                    ? ProbeTmpGlyph(probe)
                    : HasTmpGlyph(probe, false);
                bool uiToolkitGlyph = uiToolkitStatus == "ready" && legacyGlyph;
                long totalTranslations = totalExactTranslationApplications
                    + totalNormalizedTranslationApplications
                    + totalTemplateTranslationApplications;
                string missingCodepointsJson = "[]";
                if (missingCodepoints.Count > 0)
                {
                    StringBuilder missingJson = new StringBuilder("[");
                    for (int index = 0; index < missingCodepoints.Count; index++)
                    {
                        if (index > 0)
                        {
                            missingJson.Append(',');
                        }
                        missingJson.Append(missingCodepoints[index].ToString());
                    }
                    missingJson.Append(']');
                    missingCodepointsJson = missingJson.ToString();
                }
                string scenesJson = "[]";
                if (seenScenes.Count > 0)
                {
                    StringBuilder sceneJson = new StringBuilder("[");
                    for (int index = 0; index < seenScenes.Count; index++)
                    {
                        if (index > 0)
                        {
                            sceneJson.Append(',');
                        }
                        sceneJson.Append('"');
                        sceneJson.Append(EscapeJson(seenScenes[index]));
                        sceneJson.Append('"');
                    }
                    sceneJson.Append(']');
                    scenesJson = sceneJson.ToString();
                }
                string failuresJson = "[]";
                if (consumerFailures.Count > 0)
                {
                    StringBuilder failureJson = new StringBuilder("[");
                    for (int index = 0; index < consumerFailures.Count; index++)
                    {
                        if (index > 0)
                        {
                            failureJson.Append(',');
                        }
                        failureJson.Append(consumerFailures[index]);
                    }
                    failureJson.Append(']');
                    failuresJson = failureJson.ToString();
                }
                string payload = "{\"protocol_version\":" + HealthProtocolVersion
                    + ",\"plugin_version\":\""
                    + EscapeJson(PluginVersion)
                    + "\",\"session_nonce\":\""
                    + EscapeJson(sessionNonce)
                    + "\",\"last_seen\":" + lastSeenSeconds
                    + ",\"scenes\":" + scenesJson
                    + ",\"adapters\":{\"legacy\":{\"status\":\""
                    + EscapeJson(legacyStatus) + "\",\"error\":\""
                    + EscapeJson(legacyError) + "\",\"glyph\":"
                    + (legacyGlyph ? "true" : "false") + "},\"tmp\":{\"status\":\""
                    + EscapeJson(tmpStatus) + "\",\"error\":\""
                    + EscapeJson(tmpError) + "\",\"glyph\":"
                    + (tmpGlyph ? "true" : "false")
                    + "},\"uitoolkit\":{\"status\":\""
                    + EscapeJson(uiToolkitStatus) + "\",\"error\":\""
                    + EscapeJson(uiToolkitError) + "\",\"glyph\":"
                    + (uiToolkitGlyph ? "true" : "false")
                    + "}},\"glyph_probe\":\""
                    + EscapeJson(probe.ToString())
                    + "\",\"glyph_verification\":{\"snapshot_hash\":\""
                    + EscapeJson(requiredGlyphsHash)
                    + "\",\"legacy_total\":" + glyphLegacyTotal
                    + ",\"legacy_covered\":" + glyphLegacyCovered
                    + ",\"legacy_missing\":" + glyphLegacyMissing
                    + ",\"tmp_total\":" + glyphTmpTotal
                    + ",\"tmp_covered\":" + glyphTmpCovered
                    + ",\"tmp_missing\":" + glyphTmpMissing
                    + ",\"missing_codepoints\":" + missingCodepointsJson
                    + ",\"missing_total\":" + verifiedMissingTotal
                    + ",\"error\":\""
                    + EscapeJson(glyphVerificationError)
                    + "\"},\"consumers\":{\"discovered\":"
                    + consumersDiscovered + ",\"chinese\":" + consumersChinese
                    + ",\"covered\":" + consumersCovered
                    + ",\"missing\":" + consumersMissing
                    + ",\"failed\":" + consumersFailed
                    + "},\"failures\":" + failuresJson
                    + ",\"applications\":{\"tmp\":"
                    + totalTmpApplications + ",\"ui\":" + totalUiApplications
                    + ",\"uitoolkit\":" + totalUiToolkitApplications
                    + ",\"textmesh\":" + totalTextMeshApplications
                    + ",\"translations\":" + totalTranslations
                    + ",\"exact_translations\":"
                    + totalExactTranslationApplications
                    + ",\"normalized_translations\":"
                    + totalNormalizedTranslationApplications
                    + ",\"template_translations\":"
                    + totalTemplateTranslationApplications
                    + "},\"translation_targets\":{"
                    + "\"tmp\":" + TranslationTargetHealth(TranslationTargetKind.Tmp)
                    + ",\"ui\":" + TranslationTargetHealth(TranslationTargetKind.Ui)
                    + ",\"uitoolkit\":"
                    + TranslationTargetHealth(TranslationTargetKind.UiToolkit)
                    + ",\"textmesh\":"
                    + TranslationTargetHealth(TranslationTargetKind.TextMesh)
                    + "}}";
                if (string.Equals(payload, lastHealthPayload, StringComparison.Ordinal))
                {
                    return;
                }
                string target = Path.Combine(pluginDirectory, "font-health.json");
                string temporary = target + ".tmp";
                File.WriteAllText(temporary, payload, new UTF8Encoding(false));
                if (File.Exists(target))
                {
                    File.Replace(temporary, target, null);
                }
                else
                {
                    File.Move(temporary, target);
                }
                lastHealthPayload = payload;
            }
            catch (Exception exception)
            {
                Logger.LogWarning("Could not write font-health.json atomically: " + exception);
            }
        }

        // ── Phase 3：逐 scalar 证明与消费者统计 ──

        private void LoadRequiredGlyphs()
        {
            requiredGlyphs.Clear();
            requiredGlyphsHash = "";
            string path = Path.Combine(pluginDirectory, "required-glyphs.json");
            if (!File.Exists(path))
            {
                Logger.LogInfo("REQUIRED_GLYPHS_READY count=0 file=missing");
                return;
            }
            string payload = File.ReadAllText(path, new UTF8Encoding(false, true));
            int cursor = 0;
            SkipJsonWhitespace(payload, ref cursor);
            ExpectJsonCharacter(payload, ref cursor, '{');
            SkipJsonWhitespace(payload, ref cursor);
            while (cursor < payload.Length && payload[cursor] != '}')
            {
                string property = ReadJsonStringValue(payload, ref cursor);
                ExpectJsonCharacter(payload, ref cursor, ':');
                if (string.Equals(property, "scalars", StringComparison.Ordinal))
                {
                    requiredGlyphs.AddRange(ReadJsonUintArray(payload, ref cursor));
                }
                else if (string.Equals(
                    property, "snapshot_hash", StringComparison.Ordinal))
                {
                    requiredGlyphsHash = ReadJsonStringValue(payload, ref cursor);
                }
                else
                {
                    SkipJsonValue(payload, ref cursor);
                }
                SkipJsonWhitespace(payload, ref cursor);
                if (cursor < payload.Length && payload[cursor] == ',')
                {
                    cursor++;
                    SkipJsonWhitespace(payload, ref cursor);
                }
                else
                {
                    break;
                }
            }
            ExpectJsonCharacter(payload, ref cursor, '}');
            Logger.LogInfo(
                "REQUIRED_GLYPHS_READY count=" + requiredGlyphs.Count
                + " hash=" + requiredGlyphsHash);
        }

        private static void SkipJsonValue(string payload, ref int cursor)
        {
            if (cursor >= payload.Length)
            {
                return;
            }
            char first = payload[cursor];
            if (first == '"')
            {
                ReadJsonStringValue(payload, ref cursor);
                return;
            }
            if (first == '[' || first == '{')
            {
                char closing = first == '[' ? ']' : '}';
                int depth = 0;
                bool inString = false;
                while (cursor < payload.Length)
                {
                    char current = payload[cursor++];
                    if (inString)
                    {
                        if (current == '\\')
                        {
                            if (cursor < payload.Length)
                            {
                                cursor++;
                            }
                        }
                        else if (current == '"')
                        {
                            inString = false;
                        }
                        continue;
                    }
                    if (current == '"')
                    {
                        inString = true;
                    }
                    else if (current == first)
                    {
                        depth++;
                    }
                    else if (current == closing)
                    {
                        depth--;
                        if (depth == 0)
                        {
                            return;
                        }
                    }
                }
                return;
            }
            while (cursor < payload.Length
                && payload[cursor] != ',' && payload[cursor] != '}')
            {
                cursor++;
            }
        }

        private static List<uint> ReadJsonUintArray(
            string payload, ref int cursor)
        {
            List<uint> values = new List<uint>();
            ExpectJsonCharacter(payload, ref cursor, '[');
            SkipJsonWhitespace(payload, ref cursor);
            while (cursor < payload.Length && payload[cursor] != ']')
            {
                int start = cursor;
                while (cursor < payload.Length
                    && payload[cursor] != ',' && payload[cursor] != ']')
                {
                    cursor++;
                }
                string text = payload.Substring(start, cursor - start).Trim();
                uint value;
                if (text.Length == 0 || !uint.TryParse(text, out value))
                {
                    throw new FormatException(
                        "required glyph scalar is not a uint");
                }
                values.Add(value);
                SkipJsonWhitespace(payload, ref cursor);
                if (cursor < payload.Length && payload[cursor] == ',')
                {
                    cursor++;
                    SkipJsonWhitespace(payload, ref cursor);
                }
                else
                {
                    break;
                }
            }
            ExpectJsonCharacter(payload, ref cursor, ']');
            return values;
        }

        private void AddScene(string sceneName)
        {
            if (string.IsNullOrEmpty(sceneName))
            {
                return;
            }
            foreach (string existing in seenScenes)
            {
                if (string.Equals(existing, sceneName, StringComparison.Ordinal))
                {
                    return;
                }
            }
            if (seenScenes.Count < MaxScenes)
            {
                seenScenes.Add(sceneName);
            }
        }

        private void VerifyRequiredGlyphs()
        {
            if (requiredGlyphs.Count == 0)
            {
                return;
            }
            glyphLegacyTotal = glyphTmpTotal = requiredGlyphs.Count;
            glyphLegacyCovered = glyphLegacyMissing = 0;
            glyphTmpCovered = glyphTmpMissing = 0;
            List<uint> pending = new List<uint>();
            foreach (uint scalar in requiredGlyphs)
            {
                bool legacyOk = legacyCovered.Contains((int)scalar);
                bool tmpOk = tmpCovered.Contains((int)scalar);
                if (!legacyOk && !tmpOk)
                {
                    legacyOk = ProbeLegacyGlyph(scalar);
                    tmpOk = HasTmpGlyphOrTryAdd(scalar);
                    if (legacyOk)
                    {
                        legacyCovered.Add((int)scalar);
                    }
                    if (tmpOk)
                    {
                        tmpCovered.Add((int)scalar);
                    }
                }
                if (legacyOk)
                {
                    glyphLegacyCovered++;
                }
                else
                {
                    glyphLegacyMissing++;
                }
                if (tmpOk)
                {
                    glyphTmpCovered++;
                }
                else
                {
                    glyphTmpMissing++;
                    pending.Add(scalar);
                }
            }
            verifiedMissingTotal = pending.Count;
            missingCodepoints.Clear();
            for (int index = 0; index < pending.Count
                && index < MaxDetailRecords; index++)
            {
                missingCodepoints.Add(pending[index]);
            }
        }

        private bool ProbeLegacyGlyph(uint scalar)
        {
            if (dynamicFont == null)
            {
                return false;
            }
            // 非 BMP（代理对）legacy 单字符 API 无法证明，如实缺失
            string utf16 = char.ConvertFromUtf32((int)scalar);
            if (utf16.Length != 1)
            {
                return false;
            }
            try
            {
                return dynamicFont.HasCharacter(utf16[0]);
            }
            catch (Exception)
            {
                return false;
            }
        }

        private bool HasTmpGlyphOrTryAdd(uint scalar)
        {
            if (dynamicTmpFont == null)
            {
                return false;
            }
            string utf16 = char.ConvertFromUtf32((int)scalar);
            if (utf16.Length == 1)
            {
                if (HasTmpGlyph(utf16[0], true))
                {
                    return true;
                }
            }
            else
            {
                // 非 BMP：无法逐字添加，如实缺失
                return false;
            }
            foreach (MethodInfo method in dynamicTmpFont.GetType().GetMethods(
                BindingFlags.Public | BindingFlags.Instance))
            {
                if (method.Name != "TryAddCharacter"
                    && method.Name != "TryAddCharacters")
                {
                    continue;
                }
                ParameterInfo[] parameters = method.GetParameters();
                if (parameters.Length == 0)
                {
                    continue;
                }
                object first;
                Type firstType = parameters[0].ParameterType;
                if (firstType == typeof(string))
                {
                    first = utf16;
                }
                else if (firstType == typeof(char))
                {
                    first = utf16[0];
                }
                else if (firstType == typeof(int))
                {
                    first = (int)scalar;
                }
                else if (firstType == typeof(uint))
                {
                    first = scalar;
                }
                else if (firstType == typeof(uint[]))
                {
                    first = new[] { scalar };
                }
                else
                {
                    continue;
                }
                object[] arguments = new object[parameters.Length];
                arguments[0] = first;
                bool compatible = true;
                for (int index = 1; index < parameters.Length; index++)
                {
                    Type parameterType = parameters[index].ParameterType;
                    if (parameterType == typeof(bool))
                    {
                        arguments[index] = false;
                    }
                    else if (parameterType.IsByRef)
                    {
                        Type elementType = parameterType.GetElementType();
                        arguments[index] = elementType != null && elementType.IsValueType
                            ? Activator.CreateInstance(elementType)
                            : null;
                    }
                    else if (parameters[index].IsOptional)
                    {
                        // mscorlib 2.0（CLR 2.0）没有 HasDefaultValue（见
                        // 第一处同款替换注释），IsOptional 两代 CLR 都有。
                        arguments[index] = parameters[index].DefaultValue;
                    }
                    else
                    {
                        compatible = false;
                        break;
                    }
                }
                if (!compatible)
                {
                    continue;
                }
                try
                {
                    object result = method.Invoke(dynamicTmpFont, arguments);
                    if ((result is bool && (bool)result)
                        || HasTmpGlyph(utf16[0], false))
                    {
                        return true;
                    }
                }
                catch (Exception)
                {
                }
            }
            return false;
        }

        private bool IsCoveredScalar(uint scalar)
        {
            return ProbeLegacyGlyph(scalar) || HasTmpGlyphOrTryAdd(scalar);
        }

        private void CollectConsumerEvidence()
        {
            consumersDiscovered = consumersChinese = 0;
            consumersCovered = consumersMissing = consumersFailed = 0;
            consumerFailures.Clear();
            CollectConsumerObjectsForType(tmpTextType, "tmp");
            CollectConsumerObjectsForType(uiTextType, "ui");
            foreach (TextMesh textMesh in Resources.FindObjectsOfTypeAll<TextMesh>())
            {
                CollectConsumerObject(textMesh, "textmesh");
            }
            if (uiToolkitTextElementType != null)
            {
                foreach (object element in EnumerateUiToolkitTextElements())
                {
                    CollectConsumerObject(element, "uitoolkit");
                }
            }
        }

        private void CollectConsumerObjectsForType(Type type, string kind)
        {
            if (type == null)
            {
                return;
            }
            foreach (UnityEngine.Object target in FindObjectsOfOptionalType(type))
            {
                if (target == null)
                {
                    continue;
                }
                CollectConsumerObject(target, kind);
            }
        }

        private void CollectConsumerObject(object target, string kind)
        {
            if (IsTargetUnavailable(target))
            {
                return;
            }
            string text;
            try
            {
                PropertyInfo textProperty = target.GetType().GetProperty(
                    "text", BindingFlags.Public | BindingFlags.Instance);
                if (textProperty == null || !textProperty.CanRead
                    || textProperty.PropertyType != typeof(string))
                {
                    return;
                }
                text = (string)textProperty.GetValue(target, null);
            }
            catch (Exception)
            {
                consumersFailed++;
                NoteConsumerFailure(target, kind, "<read-failure>", null);
                return;
            }
            consumersDiscovered++;
            List<uint> cjk = CjkScalarsOf(text);
            if (cjk.Count == 0)
            {
                return;
            }
            consumersChinese++;
            List<uint> missing = new List<uint>();
            foreach (uint scalar in cjk)
            {
                if (!IsCoveredScalar(scalar))
                {
                    missing.Add(scalar);
                }
            }
            if (missing.Count == 0)
            {
                consumersCovered++;
            }
            else
            {
                consumersMissing++;
                NoteConsumerFailure(target, kind, FontAssetOf(target), missing);
            }
        }

        private static List<uint> CjkScalarsOf(string text)
        {
            List<uint> scalars = new List<uint>(16);
            if (string.IsNullOrEmpty(text))
            {
                return scalars;
            }
            for (int index = 0; index < text.Length; index++)
            {
                uint scalar;
                try
                {
                    scalar = (uint)char.ConvertToUtf32(text, index);
                }
                catch (ArgumentException)
                {
                    continue;
                }
                if (scalar >= 0x3400 && scalar <= 0x9fff)
                {
                    scalars.Add(scalar);
                }
                if (char.IsHighSurrogate(text[index]))
                {
                    index++;
                }
            }
            return scalars;
        }

        private string StableIdentity(object target)
        {
            string name = "";
            try
            {
                PropertyInfo nameProperty = target.GetType().GetProperty(
                    "name", BindingFlags.Public | BindingFlags.Instance);
                if (nameProperty != null && nameProperty.CanRead
                    && nameProperty.PropertyType == typeof(string))
                {
                    name = (string)nameProperty.GetValue(target, null);
                }
            }
            catch (Exception)
            {
            }
            return target.GetType().Name
                + (string.IsNullOrEmpty(name) ? "" : ":" + name);
        }

        private void NoteConsumerFailure(
            object target, string kind, string fontAsset, List<uint> missing)
        {
            if (consumerFailures.Count >= MaxDetailRecords)
            {
                return;
            }
            string missingJson = "[]";
            if (missing != null && missing.Count > 0)
            {
                StringBuilder missingText = new StringBuilder("[");
                int limit = missing.Count < MaxDetailRecords
                    ? missing.Count
                    : MaxDetailRecords;
                for (int index = 0; index < limit; index++)
                {
                    if (index > 0)
                    {
                        missingText.Append(',');
                    }
                    missingText.Append(missing[index].ToString());
                }
                missingText.Append(']');
                missingJson = missingText.ToString();
            }
            consumerFailures.Add("{\"stable_identity\":\""
                + EscapeJson(StableIdentity(target)) + "\",\"kind\":\""
                + EscapeJson(kind) + "\",\"font_asset\":\""
                + EscapeJson(fontAsset) + "\",\"missing\":"
                + missingJson + "}");
        }

        private string FontAssetOf(object target)
        {
            foreach (string propertyName in new[] { "font", "fontAsset" })
            {
                try
                {
                    PropertyInfo property = target.GetType().GetProperty(
                        propertyName, BindingFlags.Public | BindingFlags.Instance);
                    if (property == null || !property.CanRead)
                    {
                        continue;
                    }
                    object value = property.GetValue(target, null);
                    if (value == null)
                    {
                        continue;
                    }
                    PropertyInfo nameProperty = value.GetType().GetProperty(
                        "name", BindingFlags.Public | BindingFlags.Instance);
                    if (nameProperty == null || !nameProperty.CanRead)
                    {
                        continue;
                    }
                    string name = nameProperty.GetValue(value, null) as string;
                    if (!string.IsNullOrEmpty(name))
                    {
                        return name;
                    }
                }
                catch (Exception)
                {
                }
            }
            return "";
        }

        private char RepresentativeGlyph()
        {
            foreach (string translated in exactTranslations.Values)
            {
                foreach (char character in translated)
                {
                    if (character >= '\u3400' && character <= '\u9fff')
                    {
                        return character;
                    }
                }
            }
            return '\u6c49';
        }

        private bool ProbeTmpGlyph(char character)
        {
            if (dynamicTmpFont == null)
            {
                return false;
            }
            if (TryAddTmpGlyph(character))
            {
                return true;
            }
            return HasTmpGlyph(character, false);
        }

        private void EnsureUsableTmpFont()
        {
            if (tmpFactoryVerified)
            {
                return;
            }

            char character = RepresentativeGlyph();
            Logger.LogInfo("TMP_GLYPH_VALIDATION_STARTED glyph=" + character);
            List<string> failures = new List<string>();
            while (dynamicTmpFont != null)
            {
                if (ProbeTmpGlyph(character))
                {
                    tmpFactoryVerified = true;
                    tmpStatus = "ready";
                    tmpError = "";
                    Logger.LogInfo(
                        "TMP_FACTORY_READY candidate="
                        + DescribeTmpCandidate(activeTmpFactoryArgument)
                        + " glyph=" + character);
                    return;
                }

                Logger.LogWarning(
                    "TMP_FACTORY_REJECTED candidate="
                    + DescribeTmpCandidate(activeTmpFactoryArgument)
                    + " glyph=" + character);
                bool rejectedBundle = tmpFontSource == "bundle";
                DiscardDynamicTmpFont();
                if (rejectedBundle)
                {
                    try
                    {
                        InitializeTmpFont(tmpFontFamily, false);
                    }
                    catch (Exception exception)
                    {
                        failures.Add(
                            "Dynamic fallback after bundle rejection failed: "
                            + exception.GetType().Name + ": "
                            + exception.Message);
                        break;
                    }
                }
                else if (!ActivateNextTmpFont(failures))
                {
                    break;
                }
            }

            tmpStatus = "failed";
            tmpError = "No TMP factory candidate could generate glyph "
                + character;
            if (failures.Count > 0)
            {
                tmpError += ": " + string.Join(" | ", failures.ToArray());
            }
            Logger.LogError("TMP glyph validation failed: " + tmpError);
        }

        private void DiscardDynamicTmpFont()
        {
            if (dynamicTmpFont == null)
            {
                return;
            }

            UnityEngine.Object rejectedFont = dynamicTmpFont;
            bool bundleOwned = tmpFontSource == "bundle";
            try
            {
                RemoveDynamicTmpFallbacks();
            }
            catch (Exception exception)
            {
                Logger.LogWarning(
                    "TMP fallback detach failed during candidate failover: "
                    + exception);
            }
            finally
            {
                dynamicTmpFont = null;
                tmpSourceFont = null;
                activeTmpFactoryArgument = null;
                tmpFactoryVerified = false;
                tmpFontSource = "";
                try
                {
                    if (bundleOwned)
                    {
                        if (tmpFontBundle != null)
                        {
                            tmpFontBundle.Unload(false);
                            tmpFontBundle = null;
                        }
                    }
                    else
                    {
                        Destroy(rejectedFont);
                    }
                }
                catch (Exception exception)
                {
                    Logger.LogWarning(
                        "TMP rejected candidate destroy failed: " + exception);
                }
            }
        }

        private bool TryAddTmpGlyph(char character)
        {
            if (HasTmpGlyph(character, true))
            {
                return true;
            }
            foreach (MethodInfo method in dynamicTmpFont.GetType().GetMethods(
                BindingFlags.Public | BindingFlags.Instance))
            {
                if (method.Name != "TryAddCharacter"
                    && method.Name != "TryAddCharacters")
                {
                    continue;
                }
                ParameterInfo[] parameters = method.GetParameters();
                if (parameters.Length == 0)
                {
                    continue;
                }
                object first;
                Type firstType = parameters[0].ParameterType;
                if (firstType == typeof(string))
                {
                    first = character.ToString();
                }
                else if (firstType == typeof(char))
                {
                    first = character;
                }
                else if (firstType == typeof(int))
                {
                    first = (int)character;
                }
                else if (firstType == typeof(uint))
                {
                    first = (uint)character;
                }
                else if (firstType == typeof(uint[]))
                {
                    first = new[] { (uint)character };
                }
                else
                {
                    continue;
                }
                object[] arguments = new object[parameters.Length];
                arguments[0] = first;
                bool compatible = true;
                for (int index = 1; index < parameters.Length; index++)
                {
                    Type parameterType = parameters[index].ParameterType;
                    if (parameterType == typeof(bool))
                    {
                        arguments[index] = false;
                    }
                    else if (parameterType.IsByRef)
                    {
                        Type elementType = parameterType.GetElementType();
                        arguments[index] = elementType != null && elementType.IsValueType
                            ? Activator.CreateInstance(elementType)
                            : null;
                    }
                    else if (parameters[index].IsOptional)
                    {
                        // mscorlib 2.0（CLR 2.0）没有 HasDefaultValue（见
                        // 第一处同款替换注释），IsOptional 两代 CLR 都有。
                        arguments[index] = parameters[index].DefaultValue;
                    }
                    else
                    {
                        compatible = false;
                        break;
                    }
                }
                if (!compatible)
                {
                    continue;
                }
                try
                {
                    object result = method.Invoke(dynamicTmpFont, arguments);
                    if ((result is bool && (bool)result)
                        || HasTmpGlyph(character, false))
                    {
                        return true;
                    }
                }
                catch (Exception)
                {
                }
            }
            return false;
        }

        private bool HasTmpGlyph(char character, bool requestDynamicAdd)
        {
            foreach (MethodInfo method in dynamicTmpFont.GetType().GetMethods(
                BindingFlags.Public | BindingFlags.Instance))
            {
                if (method.Name != "HasCharacter")
                {
                    continue;
                }
                ParameterInfo[] parameters = method.GetParameters();
                if (parameters.Length == 0
                    || (parameters[0].ParameterType != typeof(char)
                        && parameters[0].ParameterType != typeof(int)
                        && parameters[0].ParameterType != typeof(uint)))
                {
                    continue;
                }
                object[] arguments = new object[parameters.Length];
                arguments[0] = parameters[0].ParameterType == typeof(char)
                    ? (object)character
                    : parameters[0].ParameterType == typeof(uint)
                        ? (object)(uint)character
                        : (int)character;
                bool compatible = true;
                for (int index = 1; index < parameters.Length; index++)
                {
                    if (parameters[index].ParameterType != typeof(bool))
                    {
                        compatible = false;
                        break;
                    }
                    string parameterName = parameters[index].Name ?? "";
                    arguments[index] = requestDynamicAdd
                        && parameterName.IndexOf(
                            "tryAdd", StringComparison.OrdinalIgnoreCase) >= 0;
                }
                if (!compatible)
                {
                    continue;
                }
                try
                {
                    if ((bool)method.Invoke(dynamicTmpFont, arguments))
                    {
                        return true;
                    }
                }
                catch (Exception)
                {
                }
            }
            return false;
        }

        private static string EscapeJson(string value)
        {
            StringBuilder output = new StringBuilder();
            foreach (char current in value ?? "")
            {
                switch (current)
                {
                    case '"':
                        output.Append("\\\"");
                        break;
                    case '\\':
                        output.Append("\\\\");
                        break;
                    case '\b':
                        output.Append("\\b");
                        break;
                    case '\f':
                        output.Append("\\f");
                        break;
                    case '\n':
                        output.Append("\\n");
                        break;
                    case '\r':
                        output.Append("\\r");
                        break;
                    case '\t':
                        output.Append("\\t");
                        break;
                    default:
                        if (current < ' ')
                        {
                            output.Append("\\u");
                            output.Append(((int)current).ToString("x4"));
                        }
                        else
                        {
                            output.Append(current);
                        }
                        break;
                }
            }
            return output.ToString();
        }

        private void EnsureGlobalTmpFallback()
        {
            if (dynamicTmpFont == null)
            {
                return;
            }

            IList globalFallbacks = GetGlobalTmpFallbacks(true);
            if (globalFallbacks == null)
            {
                Logger.LogWarning("TMP global fallback property is unavailable");
                return;
            }

            if (!globalFallbacks.Contains(dynamicTmpFont))
            {
                globalFallbacks.Insert(0, dynamicTmpFont);
                Logger.LogInfo("TMP_GLOBAL_FALLBACK_READY count=" + globalFallbacks.Count);
            }
        }

        private IList GetGlobalTmpFallbacks(bool create)
        {
            if (tmpSettingsType == null || tmpFontAssetType == null)
            {
                return null;
            }
            PropertyInfo property = tmpSettingsType.GetProperty(
                "fallbackFontAssets", BindingFlags.Public | BindingFlags.Static);
            if (property == null || !property.CanRead)
            {
                return null;
            }
            IList fallbacks = property.GetValue(null, null) as IList;
            if (fallbacks == null && create && property.CanWrite)
            {
                Type listType = typeof(List<>).MakeGenericType(tmpFontAssetType);
                fallbacks = Activator.CreateInstance(listType) as IList;
                property.SetValue(null, fallbacks, null);
            }
            return fallbacks;
        }

        private void RemoveDynamicTmpFallbacks()
        {
            if (dynamicTmpFont == null)
            {
                return;
            }

            IList globalFallbacks = GetGlobalTmpFallbacks(false);
            if (globalFallbacks != null)
            {
                while (globalFallbacks.Contains(dynamicTmpFont))
                {
                    globalFallbacks.Remove(dynamicTmpFont);
                }
            }

            UnityEngine.Object[] loadedFonts = FindObjectsOfOptionalType(
                tmpFontAssetType);
            foreach (UnityEngine.Object fontAsset in loadedFonts)
            {
                if (fontAsset == null || ReferenceEquals(fontAsset, dynamicTmpFont))
                {
                    continue;
                }

                try
                {
                    PropertyInfo fallbackProperty = fontAsset.GetType().GetProperty(
                        "fallbackFontAssetTable",
                        BindingFlags.Public | BindingFlags.Instance);
                    IList fallbacks = fallbackProperty == null
                        ? null
                        : fallbackProperty.GetValue(fontAsset, null) as IList;
                    if (fallbacks == null)
                    {
                        continue;
                    }

                    while (fallbacks.Contains(dynamicTmpFont))
                    {
                        fallbacks.Remove(dynamicTmpFont);
                    }
                }
                catch (Exception exception)
                {
                    Logger.LogWarning(
                        "Could not remove TMP fallback from " + fontAsset.name + ": "
                        + exception);
                }
            }
        }

        private void OnSceneLoaded(Scene scene, LoadSceneMode mode)
        {
            AddScene(scene.name);
            SafeApplyFonts("scene:" + scene.name);
        }

        private IEnumerator ScanLoop()
        {
            Logger.LogInfo("FONT_SCAN_LOOP_STARTED");
            while (true)
            {
                // 1.5.0 性能：退避间隔（连续空转 → 最长 8s 一扫；场景
                // 加载即时触发全扫，set_text 钩子即时补丁新文本）。
                yield return new WaitForSecondsRealtime(
                    _scansWithNoApply > 0
                        ? BackoffScanIntervalSeconds
                        : ScanIntervalSeconds);
                SafeApplyFonts("periodic");
            }
        }

        private void SafeApplyFonts(string reason)
        {
            try
            {
                glyphVerificationError = "";
                ApplyFonts(reason);
            }
            catch (Exception exception)
            {
                string detail = exception.Message;
                if (detail.Length > 200)
                {
                    detail = detail.Substring(0, 200);
                }
                glyphVerificationError = "font-scan-failed: " + detail;
                Logger.LogError("Font scan failed; the game will continue: " + exception);
            }
        }

        // Rendezvous 诊断 2026-08-17：主菜单中文口口（"量""盘"）多次
        // 修复无效——把所有「含中文文本」的对象（类型/内容/字体/字体
        // 类型）输出到日志，一次定位全部渲染路径。awake 场景未加载时
        // dump=0——改为每次扫描限频输出（场景加载后自动覆盖）。
        private float _lastDiagDump = -100f;
        private int _diagDumpsDone = 0;

        private void DumpTextDiagnostics()
        {
            try
            {
                UnityEngine.Object[] all = Resources.FindObjectsOfTypeAll<
                    UnityEngine.Object>();
                int dumped = 0;
                foreach (UnityEngine.Object obj in all)
                {
                    if (obj == null || dumped >= 80)
                    {
                        continue;
                    }
                    Type type = obj.GetType();
                    PropertyInfo textProp = type.GetProperty(
                        "text", BindingFlags.Public | BindingFlags.Instance);
                    if (textProp == null || !textProp.CanRead)
                    {
                        continue;
                    }
                    object value;
                    try
                    {
                        value = textProp.GetValue(obj, null);
                    }
                    catch (Exception)
                    {
                        continue;
                    }
                    string text = value as string;
                    if (string.IsNullOrEmpty(text))
                    {
                        continue;
                    }
                    bool hasChinese = false;
                    foreach (char c in text)
                    {
                        if (c >= 0x4E00 && c <= 0x9FFF)
                        {
                            hasChinese = true;
                            break;
                        }
                    }
                    if (!hasChinese)
                    {
                        continue;
                    }
                    string fontDesc = "no-font-prop";
                    string fontType = "";
                    PropertyInfo fontProp = type.GetProperty(
                        "font", BindingFlags.Public | BindingFlags.Instance);
                    if (fontProp != null && fontProp.CanRead)
                    {
                        try
                        {
                            object fv = fontProp.GetValue(obj, null);
                            fontType = fv == null ? "null" : fv.GetType().Name;
                            fontDesc = fv == null ? "null" : fv.ToString();
                        }
                        catch (Exception)
                        {
                        }
                    }
                    string preview = text.Length > 24
                        ? text.Substring(0, 24) : text;
                    Logger.LogInfo(
                        "TEXT_DIAG name=" + obj.name
                        + " type=" + type.FullName
                        + " text=" + preview
                        + " font=" + fontDesc
                        + " fontType=" + fontType);
                    dumped++;
                }
                Logger.LogInfo("TEXT_DIAG_TOTAL dumped=" + dumped);
            }
            catch (Exception exception)
            {
                Logger.LogError("TEXT_DIAG failed: " + exception);
            }
        }

        private void ApplyFonts(string reason)
        {
            // Rendezvous 实证 2026-08-17：Start 时按需程序集（UnityEngine.UI）
            // 可能未加载 → 类型发现为空 → UGUI 文本永远未 patch。每次
            // 扫描前重新发现（幂等：已发现的类型快速返回，未发现的
            // 每次尝试 Assembly.Load——场景加载后类型即被补上）。
            DiscoverOptionalTextTypes();
            if (reason == "periodic"
                || reason.StartsWith("scene:", StringComparison.Ordinal)
                || (reason == "awake" && !RequiresDeferredTmpGlyphValidation()))
            {
                EnsureUsableTmpFont();
            }
            // 1.5.0 性能：AllFontProps 大扫排程——首次（awake 后第一次
            // 扫描）与场景加载必跑；稳态周期扫描最长 30s 一跑。
            if (_allPropsScans == 0
                || reason.StartsWith("scene:", StringComparison.Ordinal))
            {
                _allPropsDue = true;
            }
            else if (Time.realtimeSinceStartup - _lastAllPropsSweep >= 30f)
            {
                _allPropsDue = true;
            }
            if (Time.realtimeSinceStartup - _lastDiagDump >= 5f)
            {
                _lastDiagDump = Time.realtimeSinceStartup;
                // 1.5.0 性能：诊断 dump 是全对象反射扫描，只跑 3 次
                // （启动定位用）；之后仅场景加载时刷新。TEXT_DIAG_TOTAL
                // dumped=0 连续出现即说明定位已完成。
                if (_diagDumpsDone < 3 || reason.StartsWith(
                        "scene:", StringComparison.Ordinal))
                {
                    _diagDumpsDone++;
                    DumpTextDiagnostics();
                }
            }
            int translatedCount = RunFontScan(
                "ExactTranslations",
                ApplyExactTranslations);
            // TMP 文本主字体替换（Rendezvous 实证：fallback 查询未生效，
            // 主字体替换绕开——官方中文字体缺字问题根治）
            int tmpTextCount = RunFontScan("TMP.Text", PatchTmpTexts);
            int tmpCount = tmpTextCount;
            tmpCount += RunFontScan("TMP", PatchLoadedTmpAssets);
            int uiCount = RunFontScan("UI.Text", PatchUiTexts);
            int uiToolkitCount = RunFontScan(
                "UI Toolkit", PatchUiToolkitTexts);
            int textMeshCount = RunFontScan("TextMesh", PatchTextMeshes);
            // 暴力兜底：一切 Font 类型属性 → 工具字体（类型白名单漏网
            // 的 UGUI/NGUI 组件靠它覆盖，Rendezvous 实证 ui=0 场景）
            // ——1.5.0 起该大扫自带退避（见 PatchAllFontProperties）。
            int allCount = RunFontScan("AllFontProps", PatchAllFontProperties);
            // 1.5.0 性能：统计本次周期/Update 扫描的空转次数（全部路径
            // 0 应用 → 计数累加用于退避；场景加载/awake 不计入，始终
            // 全量扫描，稳态恢复即时）。
            if (reason == "periodic" || reason == "update")
            {
                if (tmpCount + uiCount + uiToolkitCount
                    + textMeshCount + translatedCount + allCount > 0)
                {
                    _scansWithNoApply = 0;
                }
                else
                {
                    _scansWithNoApply = System.Math.Min(
                        _scansWithNoApply + 1, 16);
                }
            }
            uiCount += allCount;
            // IMGUI skin 只能在 OnGUI 生命周期内访问（GUI.skin 运行时
            // 检查），此处不能安全 patch（Rendezvous 实证：非 OnGUI
            // 调用抛异常跳过）——由 OnGUI() 回调专责。
            int imGuiCount = 0;
            totalTmpApplications += tmpCount;
            totalUiApplications += uiCount;
            totalUiToolkitApplications += uiToolkitCount;
            totalTextMeshApplications += textMeshCount;
            totalImGuiApplications += imGuiCount;
            VerifyRequiredGlyphs();
            CollectConsumerEvidence();
            WriteHealthManifest(reason == "periodic");
            if (reason != "periodic"
                || tmpCount + uiCount + uiToolkitCount
                    + textMeshCount + translatedCount > 0)
            {
                Logger.LogInfo(
                    "FONT_APPLY_COUNTS reason=" + reason
                    + " tmp=" + tmpCount
                    + " ui=" + uiCount
                    + " uitoolkit=" + uiToolkitCount
                    + " textmesh=" + textMeshCount
                    + " imgui=" + imGuiCount
                    + " translations=" + translatedCount
                    + " exact=" + totalExactTranslationApplications
                    + " normalized=" + totalNormalizedTranslationApplications
                    + " template=" + totalTemplateTranslationApplications
                    + " totals=" + totalTmpApplications
                    + "/" + totalUiApplications
                    + "/" + totalUiToolkitApplications
                    + "/" + totalTextMeshApplications
                    + "/" + totalImGuiApplications);
            }
        }

        private static bool RequiresDeferredTmpGlyphValidation()
        {
            string version = Application.unityVersion ?? "";
            int separator = version.IndexOf('.');
            string majorText = separator >= 0
                ? version.Substring(0, separator)
                : version;
            int major;
            return int.TryParse(majorText, out major) && major >= 6000;
        }

        private int ApplyExactTranslations()
        {
            PruneTranslationApplicationStates();
            if (exactTranslations.Count == 0)
            {
                return 0;
            }

            int applied = 0;
            applied += ApplyExactTranslationsForType(
                tmpTextType, TranslationTargetKind.Tmp);
            applied += ApplyExactTranslationsForType(
                uiTextType, TranslationTargetKind.Ui);
            applied += ApplyExactTranslationsToUiToolkit();

            TextMesh[] textMeshes = Resources.FindObjectsOfTypeAll<TextMesh>();
            foreach (TextMesh text in textMeshes)
            {
                if (text != null)
                {
                    applied += ApplyExactTranslation(
                        text, text.text,
                        delegate(string translated) { text.text = translated; },
                        TranslationTargetKind.TextMesh);
                }
            }
            return applied;
        }

        private int ApplyExactTranslationsForType(
            Type textType, TranslationTargetKind targetKind)
        {
            int applied = 0;
            foreach (UnityEngine.Object target in FindObjectsOfOptionalType(textType))
            {
                if (target == null)
                {
                    continue;
                }
                PropertyInfo textProperty = target.GetType().GetProperty(
                    "text", BindingFlags.Public | BindingFlags.Instance);
                if (textProperty == null || !textProperty.CanRead
                    || !textProperty.CanWrite
                    || textProperty.PropertyType != typeof(string))
                {
                    continue;
                }
                string current = textProperty.GetValue(target, null) as string;
                applied += ApplyExactTranslation(
                    target, current,
                    delegate(string translated)
                    {
                        textProperty.SetValue(target, translated, null);
                    },
                    targetKind);
            }
            return applied;
        }

        private int ApplyExactTranslationsToUiToolkit()
        {
            int applied = 0;
            foreach (object target in EnumerateUiToolkitTextElements())
            {
                PropertyInfo textProperty = target.GetType().GetProperty(
                    "text", BindingFlags.Public | BindingFlags.Instance);
                if (textProperty == null || !textProperty.CanRead
                    || !textProperty.CanWrite
                    || textProperty.PropertyType != typeof(string))
                {
                    continue;
                }
                string current = textProperty.GetValue(target, null) as string;
                applied += ApplyExactTranslation(
                    target, current,
                    delegate(string translated)
                    {
                        textProperty.SetValue(target, translated, null);
                    },
                    TranslationTargetKind.UiToolkit);
            }
            return applied;
        }

        private List<object> EnumerateUiToolkitTextElements()
        {
            List<object> texts = new List<object>();
            if (uiToolkitTextElementType == null || uiToolkitDocumentType == null)
            {
                return texts;
            }
            bool traversedRoot = false;
            foreach (UnityEngine.Object document in FindObjectsOfOptionalType(
                uiToolkitDocumentType))
            {
                if (document == null)
                {
                    continue;
                }
                PropertyInfo rootProperty = document.GetType().GetProperty(
                    "rootVisualElement", BindingFlags.Public | BindingFlags.Instance);
                object root = rootProperty == null
                    ? null
                    : rootProperty.GetValue(document, null);
                if (root == null)
                {
                    continue;
                }
                traversedRoot = true;
                CollectUiToolkitTextElements(root, texts);
            }
            if (traversedRoot && dynamicFont != null)
            {
                uiToolkitStatus = "ready";
                uiToolkitError = "";
            }
            return texts;
        }

        private void CollectUiToolkitTextElements(object root, List<object> texts)
        {
            Queue<object> pending = new Queue<object>();
            pending.Enqueue(root);
            while (pending.Count > 0)
            {
                object current = pending.Dequeue();
                if (uiToolkitTextElementType.IsInstanceOfType(current))
                {
                    texts.Add(current);
                }
                PropertyInfo hierarchyProperty = current.GetType().GetProperty(
                    "hierarchy", BindingFlags.Public | BindingFlags.Instance);
                object hierarchy = hierarchyProperty == null
                    ? null
                    : hierarchyProperty.GetValue(current, null);
                if (hierarchy == null)
                {
                    continue;
                }
                PropertyInfo countProperty = hierarchy.GetType().GetProperty(
                    "childCount", BindingFlags.Public | BindingFlags.Instance);
                int childCount = countProperty == null
                    ? 0
                    : Convert.ToInt32(countProperty.GetValue(hierarchy, null));
                MethodInfo elementAt = hierarchy.GetType().GetMethod(
                    "ElementAt", BindingFlags.Public | BindingFlags.Instance,
                    null, new[] { typeof(int) }, null);
                PropertyInfo itemProperty = hierarchy.GetType().GetProperty(
                    "Item", BindingFlags.Public | BindingFlags.Instance,
                    null, null, new[] { typeof(int) }, null);
                if (childCount > 0 && elementAt == null && itemProperty == null)
                {
                    throw new MissingMethodException(
                        "UI Toolkit hierarchy has no ElementAt/indexer traversal API");
                }
                for (int index = 0; index < childCount; index++)
                {
                    object child = elementAt != null
                        ? elementAt.Invoke(hierarchy, new object[] { index })
                        : itemProperty.GetValue(hierarchy, new object[] { index });
                    if (child != null)
                    {
                        pending.Enqueue(child);
                    }
                }
            }
        }

        private void PruneTranslationApplicationStates()
        {
            List<int> staleIds = new List<int>();
            foreach (KeyValuePair<int, TranslationApplicationState> pair
                in translationApplicationStates)
            {
                if (IsTargetUnavailable(pair.Value.Target))
                {
                    staleIds.Add(pair.Key);
                }
            }
            foreach (int staleId in staleIds)
            {
                translationApplicationStates.Remove(staleId);
            }
        }

        private int ApplyExactTranslation(
            object target,
            string current,
            Action<string> assign,
            TranslationTargetKind targetKind)
        {
            if (IsTargetUnavailable(target))
            {
                return 0;
            }
            UnityEngine.Object unityTarget = target as UnityEngine.Object;
#pragma warning disable 0618
            int instanceId = unityTarget != null
                ? unityTarget.GetInstanceID()
                : RuntimeHelpers.GetHashCode(target);
#pragma warning restore 0618
            TranslationApplicationState state;
            if (translationApplicationStates.TryGetValue(instanceId, out state))
            {
                if (IsTargetUnavailable(state.Target)
                    || !ReferenceEquals(state.Target, target))
                {
                    translationApplicationStates.Remove(instanceId);
                }
                else if (string.Equals(
                    current, state.LastTarget, StringComparison.Ordinal))
                {
                    return 0;
                }
                else if (!string.Equals(
                    current, state.LastSource, StringComparison.Ordinal))
                {
                    translationApplicationStates.Remove(instanceId);
                }
            }
            string translated;
            TranslationMatchMode matchMode;
            if (string.IsNullOrEmpty(current)
                || IsExcludedTranslation(current)
                || (!TryGetExactTranslation(current, out translated)
                    && !TryGetNormalizedTranslation(current, out translated)
                    && !TryGetUniqueTemplateTranslation(current, out translated))
                || string.Equals(current, translated, StringComparison.Ordinal))
            {
                return 0;
            }
            if (exactTranslations.ContainsKey(current))
            {
                matchMode = TranslationMatchMode.Exact;
            }
            else if (normalizedTranslations.ContainsKey(
                NormalizeRuntimeText(current)))
            {
                matchMode = TranslationMatchMode.Normalized;
            }
            else
            {
                matchMode = TranslationMatchMode.Template;
            }
            try
            {
                assign(translated);
                translationApplicationStates[instanceId] =
                    new TranslationApplicationState
                    {
                        Target = target,
                        LastSource = current,
                        LastTarget = translated,
                    };
                if (matchMode == TranslationMatchMode.Exact)
                {
                    totalExactTranslationApplications++;
                }
                else if (matchMode == TranslationMatchMode.Normalized)
                {
                    totalNormalizedTranslationApplications++;
                }
                else if (matchMode == TranslationMatchMode.Template)
                {
                    totalTemplateTranslationApplications++;
                }
                translationApplicationsByTargetAndMode[
                    (int)targetKind, (int)matchMode]++;
                return 1;
            }
            catch (Exception exception)
            {
                Logger.LogWarning("Exact translation assignment failed: " + exception);
                return 0;
            }
        }

        private string TranslationTargetHealth(TranslationTargetKind target)
        {
            return "{\"exact\":"
                + translationApplicationsByTargetAndMode[
                    (int)target, (int)TranslationMatchMode.Exact]
                + ",\"normalized\":"
                + translationApplicationsByTargetAndMode[
                    (int)target, (int)TranslationMatchMode.Normalized]
                + ",\"template\":"
                + translationApplicationsByTargetAndMode[
                    (int)target, (int)TranslationMatchMode.Template]
                + "}";
        }

        private bool IsExcludedTranslation(string current)
        {
            return excludedTranslations.Contains(current)
                || excludedNormalizedTranslations.Contains(
                    NormalizeRuntimeText(current));
        }

        private bool TryGetExactTranslation(string current, out string translated)
        {
            return exactTranslations.TryGetValue(current, out translated);
        }

        private bool TryGetNormalizedTranslation(
            string current, out string translated)
        {
            translated = null;
            string normalized = NormalizeRuntimeText(current);
            string normalizedTarget;
            if (conflictingNormalizedTranslations.Contains(normalized)
                || !normalizedTranslations.TryGetValue(
                    normalized, out normalizedTarget))
            {
                return false;
            }
            translated = RestoreBoundaryWhitespace(current, normalizedTarget);
            return true;
        }

        private static string RestoreBoundaryWhitespace(
            string source, string translated)
        {
            int leading = 0;
            while (leading < source.Length && char.IsWhiteSpace(source[leading]))
            {
                leading++;
            }
            int trailing = source.Length;
            while (trailing > leading && char.IsWhiteSpace(source[trailing - 1]))
            {
                trailing--;
            }
            return source.Substring(0, leading)
                + translated.Trim()
                + source.Substring(trailing);
        }

        private bool TryGetUniqueTemplateTranslation(
            string current, out string translated)
        {
            translated = null;
            int matchingTemplates = 0;
            foreach (RuntimeTranslationTemplate template in runtimeTemplates)
            {
                List<List<string>> captures = new List<List<string>>();
                string first = template.sourceFragments[0];
                if (!current.StartsWith(first, StringComparison.Ordinal))
                {
                    continue;
                }
                CollectTemplateMatches(
                    template, current, 0, first.Length,
                    new List<string>(), captures);
                if (captures.Count != 1)
                {
                    continue;
                }
                matchingTemplates++;
                if (matchingTemplates > 1)
                {
                    break;
                }
                StringBuilder rebuilt = new StringBuilder();
                for (int index = 0; index < captures[0].Count; index++)
                {
                    rebuilt.Append(template.targetFragments[index]);
                    rebuilt.Append(captures[0][index]);
                }
                rebuilt.Append(template.targetFragments[
                    template.targetFragments.Count - 1]);
                translated = rebuilt.ToString();
            }
            if (matchingTemplates != 1)
            {
                translated = null;
                return false;
            }
            return true;
        }

        private static void CollectTemplateMatches(
            RuntimeTranslationTemplate template,
            string current,
            int slotIndex,
            int position,
            List<string> captures,
            List<List<string>> matches)
        {
            if (matches.Count > 1)
            {
                return;
            }
            string anchor = template.sourceFragments[slotIndex + 1];
            bool lastSlot = slotIndex == template.slots.Count - 1;
            if (anchor.Length == 0)
            {
                if (!lastSlot)
                {
                    return;
                }
                List<string> completed = new List<string>(captures);
                completed.Add(current.Substring(position));
                matches.Add(completed);
                return;
            }
            int occurrence = current.IndexOf(
                anchor, position, StringComparison.Ordinal);
            while (occurrence >= 0)
            {
                if (!lastSlot || occurrence + anchor.Length == current.Length)
                {
                    List<string> next = new List<string>(captures);
                    next.Add(current.Substring(position, occurrence - position));
                    if (lastSlot)
                    {
                        matches.Add(next);
                    }
                    else
                    {
                        CollectTemplateMatches(
                            template, current, slotIndex + 1,
                            occurrence + anchor.Length, next, matches);
                    }
                    if (matches.Count > 1)
                    {
                        return;
                    }
                }
                occurrence = current.IndexOf(
                    anchor, occurrence + 1, StringComparison.Ordinal);
            }
        }

        private static bool IsTargetUnavailable(object target)
        {
            if (target == null)
            {
                return true;
            }
            UnityEngine.Object unityTarget = target as UnityEngine.Object;
            return !ReferenceEquals(unityTarget, null) && unityTarget == null;
        }

        private int RunFontScan(string category, Func<int> scan)
        {
            try
            {
                return scan();
            }
            catch (Exception exception)
            {
                Logger.LogError(
                    category + " font scan failed; other text systems will continue: "
                    + exception);
                return 0;
            }
        }

        // Rendezvous 实证 2026-08-17：主菜单/加载界面文本是 TMP
        // （TextMeshProUGUI，官方中文字体 asset 渲染）——官方字体只
        // 覆盖官方中文集，译文新字缺字形 → 个别字口口（每次启动相同，
        // 确定性缺字）。TMP fallback 表查询在 TMP 2.x 对动态字体未
        // 生效（挂 fallback 后缺字不变）——直接把 TMP 文本的主字体
        // 替换为动态字体（工具字体全字形），绕开 fallback 查询。
        private int PatchTmpTexts()
        {
            if (dynamicTmpFont == null || tmpTextType == null)
            {
                return 0;
            }
            int applied = 0;
            foreach (UnityEngine.Object text in FindObjectsOfOptionalType(
                tmpTextType))
            {
                if (text == null)
                {
                    continue;
                }
                try
                {
                    if (ApplyTmpFontToText(text))
                    {
                        applied++;
                    }
                }
                catch (Exception exception)
                {
                    Logger.LogWarning(
                        "Could not apply TMP text font: " + exception.Message);
                }
            }
            return applied;
        }

        private int PatchLoadedTmpAssets()
        {
            if (dynamicTmpFont == null)
            {
                return 0;
            }

            // In bundle mode the tool font is the graph root.  Original
            // assets may point to it only through the tool font's fallback
            // table; adding reverse edges here would create a cycle.
            if (tmpFontSource == "bundle")
            {
                return 0;
            }

            EnsureGlobalTmpFallback();
            int applied = 0;
            UnityEngine.Object[] loadedFonts = FindObjectsOfOptionalType(
                tmpFontAssetType);
            foreach (UnityEngine.Object fontAsset in loadedFonts)
            {
                if (fontAsset == null || ReferenceEquals(fontAsset, dynamicTmpFont))
                {
                    continue;
                }

                try
                {
                    if (IsDynamicTmpFallback(fontAsset)
                        || WouldCreateTmpFallbackCycle(fontAsset,
                            dynamicTmpFont))
                    {
                        continue;
                    }
                    PropertyInfo fallbackProperty = fontAsset.GetType().GetProperty(
                        "fallbackFontAssetTable",
                        BindingFlags.Public | BindingFlags.Instance);
                    if (fallbackProperty == null || !fallbackProperty.CanRead)
                    {
                        continue;
                    }
                    IList fallbacks = fallbackProperty.GetValue(
                        fontAsset, null) as IList;
                    if (fallbacks == null)
                    {
                        if (!fallbackProperty.CanWrite)
                        {
                            continue;
                        }
                        Type listType = typeof(List<>).MakeGenericType(
                            tmpFontAssetType);
                        fallbacks = Activator.CreateInstance(listType) as IList;
                        fallbackProperty.SetValue(fontAsset, fallbacks, null);
                    }

                    if (fallbacks.Contains(dynamicTmpFont))
                    {
                        continue;
                    }

                    fallbacks.Add(dynamicTmpFont);
                    applied++;
                }
                catch (Exception exception)
                {
                    Logger.LogWarning(
                        "Could not attach TMP fallback to " + fontAsset.name + ": " + exception);
                }
            }

            return applied;
        }

        private bool WouldCreateTmpFallbackCycle(object source,
            object candidate)
        {
            if (IsUnityObjectUnavailable(source)
                || IsUnityObjectUnavailable(candidate))
            {
                return false;
            }
            if (ReferenceEquals(source, candidate))
            {
                return true;
            }
            return CanReachTmpFallback(candidate, source,
                new HashSet<object>(ReferenceEqualityComparer.Instance));
        }

        private bool CanReachTmpFallback(object current, object target,
            HashSet<object> visited)
        {
            if (IsUnityObjectUnavailable(current)
                || !visited.Add(current))
            {
                return false;
            }
            if (ReferenceEquals(current, target))
            {
                return true;
            }

            PropertyInfo fallbackProperty = current.GetType().GetProperty(
                "fallbackFontAssetTable",
                BindingFlags.Public | BindingFlags.Instance);
            if (fallbackProperty == null || !fallbackProperty.CanRead)
            {
                return false;
            }
            IList fallbacks = fallbackProperty.GetValue(current, null) as IList;
            if (fallbacks == null)
            {
                return false;
            }
            foreach (object fallback in fallbacks)
            {
                if (CanReachTmpFallback(fallback, target, visited))
                {
                    return true;
                }
            }
            return false;
        }

        private sealed class ReferenceEqualityComparer :
            IEqualityComparer<object>
        {
            public static readonly ReferenceEqualityComparer Instance =
                new ReferenceEqualityComparer();

            public new bool Equals(object left, object right)
            {
                return ReferenceEquals(left, right);
            }

            public int GetHashCode(object value)
            {
                return RuntimeHelpers.GetHashCode(value);
            }
        }

        private bool IsDynamicTmpFallback(object candidate)
        {
            if (dynamicTmpFont == null || IsUnityObjectUnavailable(candidate))
            {
                return false;
            }
            PropertyInfo fallbackProperty = dynamicTmpFont.GetType().GetProperty(
                "fallbackFontAssetTable",
                BindingFlags.Public | BindingFlags.Instance);
            if (fallbackProperty == null || !fallbackProperty.CanRead)
            {
                return false;
            }
            IList fallbacks = fallbackProperty.GetValue(
                dynamicTmpFont, null) as IList;
            return fallbacks != null && fallbacks.Contains(candidate);
        }

        // Rendezvous 实证 2026-08-17：加载界面/存档提示等 IMGUI（OnGUI）
        // 文本用 GUI.skin 默认字体（内置 Arial 无中文字形）→ 口口口。
        // 替换 GUI.skin.font 与全部样式字体（update 轮询保证场景重建后
        // 再次替换）。
        private int PatchImGuiSkin()
        {
            // IMGUI skin 只能在 OnGUI 生命周期内安全访问（GUI.skin 运行时
            // 检查）——OnGUI() 回调专责（Rendezvous 实证 2026-08-17）。
            return 0;
        }

        // Rendezvous 实证 2026-08-17：类型白名单（UGUI/TMP/TextMesh）扫描
        // 找不到游戏文本组件（ui 恒 0，主菜单中文用游戏原字体渲染 →
        // 口口口）。兜底：遍历全部对象，反射替换一切 Font 类型属性——
        // 覆盖 UGUI/NGUI/旧 Text/TextMesh 等所有 legacy 字体路径。
        // 已替换为 dynamicFont 的对象跳过（首次大扫后为增量）。
        // 1.5.0 性能：这是最重的扫描（全对象×全属性反射），从「每次
        // ApplyFonts 必跑」改为——首次扫描 + 场景加载必跑；稳态周期
        // 扫描最长 30s 一跑（Harmony set_text 钩子已覆盖动态文本的
        // 主路径，此处只是类型白名单漏网的兜底，低频兜底即可）。
        private int _allPropsScans = 0;
        private float _lastAllPropsSweep = -1000f;
        private bool _allPropsDue;

        private int PatchAllFontProperties()
        {
            if (dynamicFont == null)
            {
                return 0;
            }
            if (!_allPropsDue)
            {
                return 0;
            }
            _allPropsScans++;
            _lastAllPropsSweep = Time.realtimeSinceStartup;
            _allPropsDue = false;
            int applied = 0;
            UnityEngine.Object[] all;
            try
            {
                all = Resources.FindObjectsOfTypeAll<UnityEngine.Object>();
            }
            catch (Exception)
            {
                return 0;
            }
            foreach (UnityEngine.Object obj in all)
            {
                if (obj == null || ReferenceEquals(obj, dynamicFont))
                {
                    continue;
                }
                Type type = obj.GetType();
                if (type == typeof(Font) || type == typeof(Texture2D)
                    || type == typeof(Material) || type == typeof(Shader))
                {
                    continue;
                }
                PropertyInfo[] props;
                try
                {
                    props = type.GetProperties(
                        BindingFlags.Public | BindingFlags.Instance);
                }
                catch (Exception)
                {
                    continue;
                }
                bool isTmpFontType = dynamicTmpFont != null;
                Type tmpFontType = isTmpFontType
                    ? dynamicTmpFont.GetType() : null;
                foreach (PropertyInfo prop in props)
                {
                    if (!prop.CanWrite || !prop.CanRead)
                    {
                        continue;
                    }
                    // legacy Font 属性 → dynamicFont；
                    // TMP_FontAsset 类型属性（TMP 文本 font）→ dynamicTmpFont
                    // （Rendezvous 实证：TMP_Text 类型发现/扫描链失效，
                    // TMP 文本主字体从未被替换——官方 SDF 字体缺字口口。
                    // 按属性值类型兜底，不依赖类型白名单。）
                    bool isLegacyFont = prop.PropertyType == typeof(Font);
                    bool isTmpFont = isTmpFontType
                        && prop.PropertyType == tmpFontType;
                    if (!isLegacyFont && !isTmpFont)
                    {
                        continue;
                    }
                    try
                    {
                        object current = prop.GetValue(obj, null);
                        object target = isLegacyFont ? (object)dynamicFont
                            : dynamicTmpFont;
                        if (current == null || ReferenceEquals(current, target))
                        {
                            continue;
                        }
                        prop.SetValue(obj, target, null);
                        applied++;
                    }
                    catch (Exception)
                    {
                        // 只读/受限属性：跳过
                    }
                }
            }
            return applied;
        }

        private int PatchUiTexts()
        {
            if (dynamicFont == null)
            {
                return 0;
            }

            int applied = 0;
            foreach (UnityEngine.Object text in FindObjectsOfOptionalType(uiTextType))
            {
                if (text == null)
                {
                    continue;
                }

                try
                {
                    PropertyInfo fontProperty = text.GetType().GetProperty(
                        "font", BindingFlags.Public | BindingFlags.Instance);
                    if (fontProperty == null || !fontProperty.CanRead
                        || !fontProperty.CanWrite
                        || fontProperty.PropertyType != typeof(Font)
                        || ReferenceEquals(
                            fontProperty.GetValue(text, null), dynamicFont))
                    {
                        continue;
                    }
                    fontProperty.SetValue(text, dynamicFont, null);
                    applied++;
                }
                catch (Exception exception)
                {
                    Logger.LogWarning("Could not apply UI.Text font: " + exception);
                }
            }

            return applied;
        }

        private int PatchUiToolkitTexts()
        {
            if (dynamicFont == null || uiToolkitTextElementType == null)
            {
                return 0;
            }
            int applied = 0;
            foreach (object text in EnumerateUiToolkitTextElements())
            {
                if (text == null)
                {
                    continue;
                }
                try
                {
                    PropertyInfo styleProperty = text.GetType().GetProperty(
                        "style", BindingFlags.Public | BindingFlags.Instance);
                    object style = styleProperty == null
                        ? null
                        : styleProperty.GetValue(text, null);
                    if (style == null)
                    {
                        continue;
                    }
                    PropertyInfo unityFontProperty = style.GetType().GetProperty(
                        "unityFont", BindingFlags.Public | BindingFlags.Instance);
                    if (unityFontProperty == null || !unityFontProperty.CanWrite)
                    {
                        continue;
                    }
                    object styleFont = CreateStyleFont(
                        unityFontProperty.PropertyType, dynamicFont);
                    if (styleFont == null)
                    {
                        continue;
                    }
                    unityFontProperty.SetValue(style, styleFont, null);
                    applied++;
                }
                catch (Exception exception)
                {
                    Logger.LogWarning(
                        "Could not apply UI Toolkit font: " + exception);
                }
            }
            return applied;
        }

        private static object CreateStyleFont(Type styleFontType, Font font)
        {
            ConstructorInfo constructor = styleFontType.GetConstructor(
                new[] { typeof(Font) });
            if (constructor != null)
            {
                return constructor.Invoke(new object[] { font });
            }
            object value = Activator.CreateInstance(styleFontType);
            PropertyInfo valueProperty = styleFontType.GetProperty(
                "value", BindingFlags.Public | BindingFlags.Instance);
            if (valueProperty != null && valueProperty.CanWrite
                && valueProperty.PropertyType == typeof(Font))
            {
                valueProperty.SetValue(value, font, null);
                return value;
            }
            FieldInfo valueField = styleFontType.GetField(
                "value", BindingFlags.Public | BindingFlags.Instance);
            if (valueField != null && valueField.FieldType == typeof(Font))
            {
                valueField.SetValue(value, font);
                return value;
            }
            return null;
        }

        private int PatchTextMeshes()
        {
            if (dynamicFont == null)
            {
                return 0;
            }

            int applied = 0;
            TextMesh[] textMeshes = Resources.FindObjectsOfTypeAll<TextMesh>();
            foreach (TextMesh textMesh in textMeshes)
            {
                if (textMesh == null)
                {
                    continue;
                }

                try
                {
                    MeshRenderer renderer = textMesh.GetComponent<MeshRenderer>();
                    bool fontMatches = textMesh.font == dynamicFont;
                    bool materialMatches = renderer == null
                        || renderer.sharedMaterial == dynamicFont.material;
                    if (fontMatches && materialMatches)
                    {
                        continue;
                    }

                    textMesh.font = dynamicFont;
                    if (renderer != null)
                    {
                        renderer.sharedMaterial = dynamicFont.material;
                    }

                    applied++;
                }
                catch (Exception exception)
                {
                    Logger.LogWarning("Could not apply TextMesh font: " + exception);
                }
            }

            return applied;
        }
    }
}
