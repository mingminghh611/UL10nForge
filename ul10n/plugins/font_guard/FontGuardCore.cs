using System;
using System.Collections;
using System.Collections.Generic;
using System.IO;
using System.IO.Compression;
using System.Reflection;
using System.Text;

namespace UL10nForge.FontGuard
{
    internal sealed class FontGuardConfig
    {
        public string GameRoot = "";
        public string FontTtf = "";
        public string OsFont = "Microsoft YaHei";
        public string LegacySdf = "";
        public bool EnableDynamicTmp = true;
        public bool EnableLegacySdf = true;
        public bool EnableUiToolkit = true;
        public bool EnableLegacyTextFix = true;

        public static FontGuardConfig Load(string iniPath, string fallbackGameRoot)
        {
            FontGuardConfig cfg = new FontGuardConfig();
            cfg.GameRoot = fallbackGameRoot ?? "";
            if (string.IsNullOrEmpty(iniPath) || !File.Exists(iniPath))
            {
                return cfg;
            }
            try
            {
                string[] lines = File.ReadAllLines(iniPath, Encoding.UTF8);
                for (int i = 0; i < lines.Length; i++)
                {
                    string raw = lines[i];
                    if (string.IsNullOrEmpty(raw)) continue;
                    string line = raw.Trim();
                    if (line.Length == 0 || line[0] == '#' || line[0] == ';' || line[0] == '[') continue;
                    int eq = line.IndexOf('=');
                    if (eq <= 0) continue;
                    string k = line.Substring(0, eq).Trim();
                    string v = line.Substring(eq + 1).Trim();
                    if (k.Equals("GameRoot", StringComparison.OrdinalIgnoreCase) && v.Length > 0) cfg.GameRoot = v;
                    else if (k.Equals("FontTtf", StringComparison.OrdinalIgnoreCase)) cfg.FontTtf = v;
                    else if (k.Equals("OsFont", StringComparison.OrdinalIgnoreCase) && v.Length > 0) cfg.OsFont = v;
                    else if (k.Equals("LegacySdf", StringComparison.OrdinalIgnoreCase)) cfg.LegacySdf = v;
                    else if (k.Equals("EnableDynamicTmp", StringComparison.OrdinalIgnoreCase)) cfg.EnableDynamicTmp = !v.Equals("False", StringComparison.OrdinalIgnoreCase);
                    else if (k.Equals("EnableLegacySdf", StringComparison.OrdinalIgnoreCase)) cfg.EnableLegacySdf = !v.Equals("False", StringComparison.OrdinalIgnoreCase);
                    else if (k.Equals("EnableUiToolkit", StringComparison.OrdinalIgnoreCase)) cfg.EnableUiToolkit = !v.Equals("False", StringComparison.OrdinalIgnoreCase);
                    else if (k.Equals("EnableLegacyTextFix", StringComparison.OrdinalIgnoreCase)) cfg.EnableLegacyTextFix = !v.Equals("False", StringComparison.OrdinalIgnoreCase);
                }
            }
            catch
            {
            }
            return cfg;
        }

        public string ResolvePath(string relOrAbs)
        {
            if (string.IsNullOrEmpty(relOrAbs)) return "";
            if (Path.IsPathRooted(relOrAbs)) return relOrAbs;
            if (string.IsNullOrEmpty(GameRoot)) return relOrAbs;
            return Path.Combine(GameRoot, relOrAbs);
        }
    }

    internal static class FontGuardCore
    {
        private static readonly Dictionary<string, Type> TypeCache = new Dictionary<string, Type>(StringComparer.Ordinal);
        private static readonly Dictionary<int, object> OsFontCache = new Dictionary<int, object>();
        private static readonly List<object> ManagedTmpFallbacks = new List<object>();
        private static object _managedUiToolkitFont;
        private static bool _dynamicAttempted;
        private static bool _legacySdfAttempted;
        private static bool _uiToolkitAttempted;
        private static bool _loggedTmpSummary;
        private static FontGuardConfig _config;
        private static Action<string> _logInfo;
        private static Action<string> _logWarn;
        private static MethodInfo _unityOpEquality;
        private static bool _unityOpEqualityChecked;
        private static MethodInfo _il2CppTypeFrom;
        private static bool _il2CppTypeChecked;

        public static void Initialize(FontGuardConfig config, Action<string> logInfo, Action<string> logWarn)
        {
            _config = config ?? new FontGuardConfig();
            _logInfo = logInfo ?? delegate { };
            _logWarn = logWarn ?? delegate { };
        }

        public static void RunGuardPass(bool forceRefreshMeshes)
        {
            if (_config == null) return;
            try { GuardTextMeshPro(forceRefreshMeshes); }
            catch (Exception ex) { SafeWarn("TMP guard skipped: " + ex.Message); }

            if (_config.EnableUiToolkit)
            {
                try { GuardUiToolkit(); }
                catch (Exception ex) { SafeWarn("UI Toolkit guard skipped: " + ex.Message); }
            }

            if (_config.EnableLegacyTextFix)
            {
                try { GuardLegacyTextComponents(); }
                catch (Exception ex) { SafeWarn("Legacy text guard skipped: " + ex.Message); }
            }
        }

        private static void SafeInfo(string msg)
        {
            try { if (_logInfo != null) _logInfo("[UL10nForge.FontGuard] " + msg); } catch { }
        }

        private static void SafeWarn(string msg)
        {
            try { if (_logWarn != null) _logWarn("[UL10nForge.FontGuard] " + msg); } catch { }
        }

        // ── Reflection & Mono/IL2CPP Interop Helpers ────────────────────────

        public static Type FindType(string fullName)
        {
            if (string.IsNullOrEmpty(fullName)) return null;
            Type cached;
            if (TypeCache.TryGetValue(fullName, out cached))
            {
                return cached;
            }
            Assembly[] asms = AppDomain.CurrentDomain.GetAssemblies();
            for (int i = 0; i < asms.Length; i++)
            {
                try
                {
                    Type t = asms[i].GetType(fullName, false);
                    if (t != null)
                    {
                        TypeCache[fullName] = t;
                        return t;
                    }
                }
                catch
                {
                }
            }
            return null;
        }

        private static bool IsUnityNull(object obj)
        {
            if (obj == null) return true;
            try
            {
                Type uObj = FindType("UnityEngine.Object");
                if (uObj != null && uObj.IsInstanceOfType(obj))
                {
                    if (!_unityOpEqualityChecked)
                    {
                        _unityOpEqualityChecked = true;
                        _unityOpEquality = uObj.GetMethod("op_Equality", BindingFlags.Public | BindingFlags.Static, null, new Type[] { uObj, uObj }, null);
                    }
                    if (_unityOpEquality != null)
                    {
                        object eq = _unityOpEquality.Invoke(null, new object[] { obj, null });
                        if (eq is bool && (bool)eq) return true;
                    }
                    if (obj.Equals(null)) return true;
                }
            }
            catch
            {
            }
            return false;
        }

        private static void DontDestroy(object obj)
        {
            if (IsUnityNull(obj)) return;
            try
            {
                Type uObj = FindType("UnityEngine.Object");
                if (uObj == null) return;
                MethodInfo mi = uObj.GetMethod("DontDestroyOnLoad", BindingFlags.Public | BindingFlags.Static, null, new Type[] { uObj }, null);
                if (mi != null) mi.Invoke(null, new object[] { obj });
            }
            catch
            {
            }
        }

        private static object ToUnityTypeArg(Type clrType, Type expectedParamType)
        {
            if (clrType == null) return null;
            if (expectedParamType == typeof(Type) || expectedParamType.IsAssignableFrom(typeof(Type)))
            {
                return clrType;
            }
            if (!_il2CppTypeChecked)
            {
                _il2CppTypeChecked = true;
                Type il2 = FindType("Il2CppInterop.Runtime.Il2CppType");
                if (il2 != null)
                {
                    _il2CppTypeFrom = il2.GetMethod("From", BindingFlags.Public | BindingFlags.Static, null, new Type[] { typeof(Type) }, null);
                }
            }
            if (_il2CppTypeFrom != null)
            {
                return _il2CppTypeFrom.Invoke(null, new object[] { clrType });
            }
            return clrType;
        }

        private static object ConvertArrayArg(Array managedArray, Type expectedParamType)
        {
            if (managedArray == null) return null;
            if (expectedParamType.IsInstanceOfType(managedArray)) return managedArray;
            try
            {
                MethodInfo op = expectedParamType.GetMethod("op_Implicit", BindingFlags.Public | BindingFlags.Static, null, new Type[] { managedArray.GetType() }, null);
                if (op != null) return op.Invoke(null, new object[] { managedArray });
            }
            catch
            {
            }
            try
            {
                ConstructorInfo ctor = expectedParamType.GetConstructor(new Type[] { managedArray.GetType() });
                if (ctor != null) return ctor.Invoke(new object[] { managedArray });
            }
            catch
            {
            }
            return managedArray;
        }

        private static object GetPropOrField(object instance, Type type, string name)
        {
            if (type == null) return null;
            BindingFlags flags = (instance == null ? BindingFlags.Static : BindingFlags.Instance) | BindingFlags.Public | BindingFlags.NonPublic;
            try
            {
                PropertyInfo p = type.GetProperty(name, flags);
                if (p != null && p.CanRead && p.GetIndexParameters().Length == 0)
                {
                    return p.GetValue(instance, null);
                }
            }
            catch
            {
            }
            try
            {
                FieldInfo f = type.GetField(name, flags);
                if (f != null) return f.GetValue(instance);
            }
            catch
            {
            }
            return null;
        }

        private static bool SetPropOrField(object instance, Type type, string name, object value)
        {
            if (type == null) return false;
            BindingFlags flags = (instance == null ? BindingFlags.Static : BindingFlags.Instance) | BindingFlags.Public | BindingFlags.NonPublic;
            try
            {
                PropertyInfo p = type.GetProperty(name, flags);
                if (p != null && p.CanWrite && p.GetIndexParameters().Length == 0)
                {
                    object coerced = CoerceValue(value, p.PropertyType);
                    p.SetValue(instance, coerced, null);
                    return true;
                }
            }
            catch
            {
            }
            try
            {
                FieldInfo f = type.GetField(name, flags);
                if (f != null)
                {
                    object coerced = CoerceValue(value, f.FieldType);
                    f.SetValue(instance, coerced);
                    return true;
                }
            }
            catch
            {
            }
            return false;
        }

        private static object CoerceValue(object value, Type targetType)
        {
            if (value == null) return null;
            if (targetType.IsInstanceOfType(value)) return value;
            if (targetType.IsEnum)
            {
                return Enum.ToObject(targetType, Convert.ToInt32(value));
            }
            if (targetType == typeof(float)) return Convert.ToSingle(value);
            if (targetType == typeof(int)) return Convert.ToInt32(value);
            if (targetType == typeof(bool)) return Convert.ToBoolean(value);
            return value;
        }

        private static List<object> SnapshotList(object listObj)
        {
            List<object> result = new List<object>();
            if (listObj == null) return result;
            Array arr = listObj as Array;
            if (arr != null)
            {
                for (int i = 0; i < arr.Length; i++) result.Add(arr.GetValue(i));
                return result;
            }
            Type t = listObj.GetType();
            try
            {
                PropertyInfo countProp = t.GetProperty("Count", BindingFlags.Public | BindingFlags.Instance)
                    ?? t.GetProperty("Length", BindingFlags.Public | BindingFlags.Instance);
                MethodInfo getItem = t.GetMethod("get_Item", BindingFlags.Public | BindingFlags.Instance, null, new Type[] { typeof(int) }, null);
                if (countProp != null && getItem != null)
                {
                    int n = Convert.ToInt32(countProp.GetValue(listObj, null));
                    for (int i = 0; i < n; i++)
                    {
                        result.Add(getItem.Invoke(listObj, new object[] { i }));
                    }
                    return result;
                }
            }
            catch
            {
            }
            IEnumerable en = listObj as IEnumerable;
            if (en != null)
            {
                foreach (object item in en) result.Add(item);
            }
            return result;
        }

        private static bool ListContainsUnityObj(object listObj, object target)
        {
            if (listObj == null || IsUnityNull(target)) return false;
            List<object> snap = SnapshotList(listObj);
            for (int i = 0; i < snap.Count; i++)
            {
                object cur = snap[i];
                if (cur == null) continue;
                if (object.ReferenceEquals(cur, target) || cur.Equals(target)) return true;
            }
            return false;
        }

        private static bool ListInsertItem(object listObj, int index, object item)
        {
            if (listObj == null || IsUnityNull(item)) return false;
            if (ListContainsUnityObj(listObj, item)) return false;
            try
            {
                MethodInfo insMi = listObj.GetType().GetMethod("Insert", BindingFlags.Public | BindingFlags.Instance);
                if (insMi != null)
                {
                    insMi.Invoke(listObj, new object[] { index, item });
                    return true;
                }
            }
            catch
            {
            }
            return ListAddItem(listObj, item);
        }

        private static bool ListAddItem(object listObj, object item)
        {
            if (listObj == null || IsUnityNull(item)) return false;
            if (ListContainsUnityObj(listObj, item)) return false;
            try
            {
                MethodInfo addMi = listObj.GetType().GetMethod("Add", BindingFlags.Public | BindingFlags.Instance);
                if (addMi != null)
                {
                    addMi.Invoke(listObj, new object[] { item });
                    return true;
                }
            }
            catch
            {
            }
            return false;
        }

        private static List<object> FindAllLoadedObjects(Type targetType)
        {
            List<object> empty = new List<object>();
            if (targetType == null) return empty;
            try
            {
                Type resType = FindType("UnityEngine.Resources");
                if (resType != null)
                {
                    MethodInfo[] methods = resType.GetMethods(BindingFlags.Public | BindingFlags.Static);
                    for (int i = 0; i < methods.Length; i++)
                    {
                        MethodInfo m = methods[i];
                        if (m.Name != "FindObjectsOfTypeAll" || m.IsGenericMethod) continue;
                        ParameterInfo[] ps = m.GetParameters();
                        if (ps.Length != 1) continue;
                        object typeArg = ToUnityTypeArg(targetType, ps[0].ParameterType);
                        object raw = m.Invoke(null, new object[] { typeArg });
                        return SnapshotList(raw);
                    }
                }
            }
            catch
            {
            }
            return empty;
        }

        private static List<object> FindSceneObjects(Type targetType)
        {
            List<object> empty = new List<object>();
            if (targetType == null) return empty;
            try
            {
                Type uObj = FindType("UnityEngine.Object");
                if (uObj != null)
                {
                    MethodInfo[] methods = uObj.GetMethods(BindingFlags.Public | BindingFlags.Static);
                    for (int i = 0; i < methods.Length; i++)
                    {
                        MethodInfo m = methods[i];
                        if (m.Name != "FindObjectsOfType" || m.IsGenericMethod) continue;
                        ParameterInfo[] ps = m.GetParameters();
                        if (ps.Length != 1) continue;
                        object typeArg = ToUnityTypeArg(targetType, ps[0].ParameterType);
                        object raw = m.Invoke(null, new object[] { typeArg });
                        return SnapshotList(raw);
                    }
                }
            }
            catch
            {
            }
            return empty;
        }

        private static char FindFirstCjkChar(string text)
        {
            if (string.IsNullOrEmpty(text)) return '\0';
            for (int i = 0; i < text.Length; i++)
            {
                char c = text[i];
                if ((c >= 0x4E00 && c <= 0x9FFF) || (c >= 0x3400 && c <= 0x4DBF) || (c >= 0x3000 && c <= 0x303F))
                {
                    return c;
                }
            }
            return '\0';
        }

        // ── TextMeshPro Guard (Shader Healing + Dynamic TTF/OS + Legacy 1.x SDF) ──

        private static void GuardTextMeshPro(bool forceRefreshMeshes)
        {
            Type tmpFontType = FindType("TMPro.TMP_FontAsset");
            if (tmpFontType == null) return;
            Type tmpSettingsType = FindType("TMPro.TMP_Settings");
            Type fontType = FindType("UnityEngine.Font");

            object liveShader = FindLiveTmpShader(tmpSettingsType, tmpFontType);
            object fallbackList = GetOrInitTmpSettingsFallbackList(tmpSettingsType);

            // Stage A: Heal shaders on any already-loaded fallback fonts and count valid static glyphs
            int bestExistingGlyphCount = 0;
            bool hasDynamicFallback = false;
            if (fallbackList != null)
            {
                List<object> existing = SnapshotList(fallbackList);
                for (int i = 0; i < existing.Count; i++)
                {
                    object f = existing[i];
                    if (IsUnityNull(f)) continue;
                    HealTmpMaterialShader(f, liveShader);
                    // Ensure legacy TMP 1.0–1.3 fontAssetType is marked SDF (1) when glyphs are loaded
                    object fat = GetPropOrField(f, f.GetType(), "fontAssetType");
                    if (fat != null)
                    {
                        try { if (Convert.ToInt32(fat) == 0 && GetTmpGlyphCount(f) > 0) SetPropOrField(f, f.GetType(), "fontAssetType", 1); } catch { }
                    }
                    if (IsDynamicTmpFont(f)) hasDynamicFallback = true;
                    int gc = GetTmpGlyphCount(f);
                    if (gc > bestExistingGlyphCount) bestExistingGlyphCount = gc;
                }
            }

            bool addedAny = false;

            // Stage B: Dynamic multi-atlas TTF + OS fallback (TMP 1.4+ / 2.x / 3.x / Unity 6000)
            if (_config.EnableDynamicTmp && !_dynamicAttempted && HasCreateFontAsset(tmpFontType))
            {
                _dynamicAttempted = true;
                string ttfPath = _config.ResolvePath(_config.FontTtf);
                if (!string.IsNullOrEmpty(ttfPath) && File.Exists(ttfPath))
                {
                    object dynChosen = CreateDynamicTmpFromFile(tmpFontType, fontType, ttfPath, liveShader);
                    if (!IsUnityNull(dynChosen))
                    {
                        ManagedTmpFallbacks.Add(dynChosen);
                        hasDynamicFallback = true;
                        SafeInfo("Dynamic TTF TMP fallback ready: " + Path.GetFileName(ttfPath));
                    }
                }

                // Secondary OS CJK dynamic fallback catches any missing glyph/symbol not in chosen TTF
                object dynOs = CreateDynamicTmpOsFallback(tmpFontType, fontType, liveShader);
                if (!IsUnityNull(dynOs))
                {
                    ManagedTmpFallbacks.Add(dynOs);
                    hasDynamicFallback = true;
                    SafeInfo("Dynamic OS CJK TMP fallback ready.");
                }
            }

            // Stage C: Legacy TMP 1.0–1.3 runtime SDF loader (Unity 5.x / 2017 / 2018 / 2019 TMP 1.3)
            if (_config.EnableLegacySdf && !_legacySdfAttempted && !hasDynamicFallback && bestExistingGlyphCount < 100)
            {
                _legacySdfAttempted = true;
                string sdfPath = _config.ResolvePath(_config.LegacySdf);
                if (!string.IsNullOrEmpty(sdfPath) && File.Exists(sdfPath))
                {
                    object legacyFont = CreateLegacyTmpFromSdfPack(tmpFontType, sdfPath, liveShader);
                    if (!IsUnityNull(legacyFont))
                    {
                        ManagedTmpFallbacks.Add(legacyFont);
                        SafeInfo("Legacy TMP 1.x SDF fallback constructed in-memory (" + GetTmpGlyphCount(legacyFont) + " glyphs).");
                    }
                }
            }

            // Stage D: Ensure all managed fallbacks stay registered in TMP_Settings (or per-font fallback tables)
            if (ManagedTmpFallbacks.Count > 0)
            {
                if (fallbackList != null)
                {
                    // If no large pre-baked static SDF bundle is present (e.g. Unity 6000.0 OS fallback),
                    // put our chosen dynamic TTF at index 0 so the user's chosen typeface takes priority!
                    bool preferFront = bestExistingGlyphCount < 1000;
                    for (int i = 0; i < ManagedTmpFallbacks.Count; i++)
                    {
                        object mf = ManagedTmpFallbacks[i];
                        if (IsUnityNull(mf)) continue;
                        HealTmpMaterialShader(mf, liveShader);
                        bool inserted = (preferFront && i == 0)
                            ? ListInsertItem(fallbackList, 0, mf)
                            : ListAddItem(fallbackList, mf);
                        if (inserted) addedAny = true;
                    }
                }
                else
                {
                    // Game has no TMP_Settings.asset in Resources — attach fallbacks directly to loaded TMP_FontAssets
                    List<object> allFonts = FindAllLoadedObjects(tmpFontType);
                    for (int i = 0; i < allFonts.Count; i++)
                    {
                        object gameFont = allFonts[i];
                        if (IsUnityNull(gameFont) || IsManagedTmpFont(gameFont)) continue;
                        object perList = GetOrInitPerFontFallbackList(gameFont);
                        if (perList == null) continue;
                        for (int j = 0; j < ManagedTmpFallbacks.Count; j++)
                        {
                            if (ListAddItem(perList, ManagedTmpFallbacks[j])) addedAny = true;
                        }
                    }
                }
            }

            if (addedAny || forceRefreshMeshes)
            {
                RefreshActiveTmpTexts();
            }

            if (!_loggedTmpSummary)
            {
                _loggedTmpSummary = true;
                int finalCount = fallbackList != null ? SnapshotList(fallbackList).Count : ManagedTmpFallbacks.Count;
                int finalBest = bestExistingGlyphCount;
                for (int i = 0; i < ManagedTmpFallbacks.Count; i++)
                {
                    int gc = GetTmpGlyphCount(ManagedTmpFallbacks[i]);
                    if (gc > finalBest) finalBest = gc;
                }
                SafeInfo("TMP guard summary: fallbacks=" + finalCount + ", bestStaticGlyphs=" + finalBest + ", dynamic=" + hasDynamicFallback);
            }
        }

        private static bool IsManagedTmpFont(object fontAsset)
        {
            for (int i = 0; i < ManagedTmpFallbacks.Count; i++)
            {
                if (object.ReferenceEquals(ManagedTmpFallbacks[i], fontAsset) || ManagedTmpFallbacks[i].Equals(fontAsset))
                {
                    return true;
                }
            }
            return false;
        }

        private static object GetOrInitTmpSettingsFallbackList(Type tmpSettingsType)
        {
            if (tmpSettingsType == null) return null;
            try
            {
                object inst = GetPropOrField(null, tmpSettingsType, "instance");
                if (IsUnityNull(inst)) return null;
                object list = GetPropOrField(null, tmpSettingsType, "fallbackFontAssets");
                if (list != null) return list;
                FieldInfo field = tmpSettingsType.GetField("m_fallbackFontAssets", BindingFlags.Instance | BindingFlags.NonPublic | BindingFlags.Public);
                if (field != null)
                {
                    object created = Activator.CreateInstance(field.FieldType);
                    field.SetValue(inst, created);
                    return created;
                }
            }
            catch
            {
            }
            return null;
        }

        private static object GetOrInitPerFontFallbackList(object fontAsset)
        {
            if (IsUnityNull(fontAsset)) return null;
            Type t = fontAsset.GetType();
            string[] propNames = new string[] { "fallbackFontAssetTable", "fallbackFontAssets" };
            for (int i = 0; i < propNames.Length; i++)
            {
                object list = GetPropOrField(fontAsset, t, propNames[i]);
                if (list != null) return list;
            }
            string[] fieldNames = new string[] { "m_FallbackFontAssetTable", "fallbackFontAssets" };
            for (int i = 0; i < fieldNames.Length; i++)
            {
                try
                {
                    FieldInfo f = t.GetField(fieldNames[i], BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic);
                    if (f != null)
                    {
                        object created = Activator.CreateInstance(f.FieldType);
                        f.SetValue(fontAsset, created);
                        return created;
                    }
                }
                catch
                {
                }
            }
            return null;
        }

        private static object FindLiveTmpShader(Type tmpSettingsType, Type tmpFontType)
        {
            // 1. Live shader from TMP_Settings.defaultFontAsset.material.shader
            try
            {
                if (tmpSettingsType != null)
                {
                    object defFont = GetPropOrField(null, tmpSettingsType, "defaultFontAsset");
                    object sh = ExtractSupportedShaderFromFont(defFont);
                    if (!IsUnityNull(sh)) return sh;
                }
            }
            catch
            {
            }

            // 2. Live shader from any loaded TMP_FontAsset in the game
            try
            {
                List<object> loaded = FindAllLoadedObjects(tmpFontType);
                for (int i = 0; i < loaded.Count; i++)
                {
                    object sh = ExtractSupportedShaderFromFont(loaded[i]);
                    if (!IsUnityNull(sh)) return sh;
                }
            }
            catch
            {
            }

            // 3. Shader.Find fallback
            Type shaderType = FindType("UnityEngine.Shader");
            if (shaderType != null)
            {
                MethodInfo findMi = shaderType.GetMethod("Find", BindingFlags.Public | BindingFlags.Static, null, new Type[] { typeof(string) }, null);
                if (findMi != null)
                {
                    string[] names = new string[] { "TextMeshPro/Distance Field", "TextMeshPro/Mobile/Distance Field" };
                    for (int i = 0; i < names.Length; i++)
                    {
                        try
                        {
                            object sh = findMi.Invoke(null, new object[] { names[i] });
                            if (IsShaderSupported(sh)) return sh;
                        }
                        catch
                        {
                        }
                    }
                }
            }
            return null;
        }

        private static object ExtractSupportedShaderFromFont(object fontAsset)
        {
            if (IsUnityNull(fontAsset)) return null;
            object mat = GetPropOrField(fontAsset, fontAsset.GetType(), "material");
            if (IsUnityNull(mat)) return null;
            object sh = GetPropOrField(mat, mat.GetType(), "shader");
            if (IsShaderSupported(sh)) return sh;
            return null;
        }

        private static bool IsShaderSupported(object shader)
        {
            if (IsUnityNull(shader)) return false;
            try
            {
                object nameObj = GetPropOrField(shader, shader.GetType(), "name");
                string nm = nameObj as string ?? "";
                if (nm.IndexOf("InternalErrorShader", StringComparison.OrdinalIgnoreCase) >= 0) return false;
                object sup = GetPropOrField(shader, shader.GetType(), "isSupported");
                if (sup is bool) return (bool)sup;
                return true;
            }
            catch
            {
                return false;
            }
        }

        private static void HealTmpMaterialShader(object fontAsset, object liveShader)
        {
            if (IsUnityNull(fontAsset) || IsUnityNull(liveShader)) return;
            try
            {
                object mat = GetPropOrField(fontAsset, fontAsset.GetType(), "material");
                if (IsUnityNull(mat)) return;
                object curShader = GetPropOrField(mat, mat.GetType(), "shader");
                if (!IsShaderSupported(curShader))
                {
                    SetPropOrField(mat, mat.GetType(), "shader", liveShader);
                    SafeInfo("Healed unsupported TMP fallback shader using game's native TMP shader.");
                }
            }
            catch
            {
            }
        }

        private static bool IsDynamicTmpFont(object fontAsset)
        {
            if (IsUnityNull(fontAsset)) return false;
            object mode = GetPropOrField(fontAsset, fontAsset.GetType(), "atlasPopulationMode");
            if (mode != null)
            {
                try { return Convert.ToInt32(mode) == 1; } catch { }
            }
            return false;
        }

        private static int GetTmpGlyphCount(object fontAsset)
        {
            if (IsUnityNull(fontAsset)) return 0;
            Type t = fontAsset.GetType();
            object ct = GetPropOrField(fontAsset, t, "characterTable");
            if (ct != null)
            {
                int n = SnapshotList(ct).Count;
                if (n > 0) return n;
            }
            object gl = GetPropOrField(fontAsset, t, "m_glyphInfoList");
            if (gl != null)
            {
                int n = SnapshotList(gl).Count;
                if (n > 0) return n;
            }
            object cd = GetPropOrField(fontAsset, t, "characterDictionary");
            if (cd != null)
            {
                object cnt = GetPropOrField(cd, cd.GetType(), "Count");
                if (cnt != null)
                {
                    try { return Convert.ToInt32(cnt); } catch { }
                }
            }
            return 0;
        }

        private static bool HasCreateFontAsset(Type tmpFontType)
        {
            if (tmpFontType == null) return false;
            MethodInfo[] ms = tmpFontType.GetMethods(BindingFlags.Public | BindingFlags.Static);
            for (int i = 0; i < ms.Length; i++)
            {
                if (ms[i].Name == "CreateFontAsset") return true;
            }
            return false;
        }

        private static object CreateDynamicTmpFromFile(Type tmpFontType, Type fontType, string filePath, object liveShader)
        {
            if (tmpFontType == null || string.IsNullOrEmpty(filePath) || !File.Exists(filePath)) return null;
            object created = null;
            object srcFont = null;

            // Strategy 1: TMP 3.2+ / Unity 6000 direct file-path CreateFontAsset(string, int, int, int, GlyphRenderMode, int, int[, bool])
            MethodInfo[] methods = tmpFontType.GetMethods(BindingFlags.Public | BindingFlags.Static);
            for (int i = 0; i < methods.Length && IsUnityNull(created); i++)
            {
                MethodInfo m = methods[i];
                if (m.Name != "CreateFontAsset") continue;
                ParameterInfo[] ps = m.GetParameters();
                if (ps.Length >= 7 && ps.Length <= 8 && ps[0].ParameterType == typeof(string) && ps[1].ParameterType == typeof(int))
                {
                    try
                    {
                        object[] args = new object[ps.Length];
                        args[0] = filePath;
                        args[1] = 0;   // faceIndex
                        args[2] = 44;  // samplingPointSize
                        args[3] = 5;   // atlasPadding
                        args[4] = CoerceValue(4165, ps[4].ParameterType); // GlyphRenderMode.SDFAA
                        args[5] = 1024; // atlasWidth
                        args[6] = 1024; // atlasHeight
                        if (ps.Length == 8) args[7] = true; // enableMultiAtlasSupport
                        created = m.Invoke(null, args);
                    }
                    catch
                    {
                    }
                }
            }

            // Strategy 2: TMP 1.4 / 2.x / 3.x / Unity 6000 via new Font(filePath) -> CreateFontAsset(Font, ...)
            if (IsUnityNull(created) && fontType != null)
            {
                try
                {
                    ConstructorInfo ctor = fontType.GetConstructor(new Type[] { typeof(string) });
                    if (ctor != null)
                    {
                        srcFont = ctor.Invoke(new object[] { filePath });
                    }
                }
                catch
                {
                    srcFont = null;
                }

                if (!IsUnityNull(srcFont))
                {
                    // Prefer 8-arg (with multiAtlas=true), then 7-arg, then 1-arg
                    int[] arities = new int[] { 8, 7, 1 };
                    for (int a = 0; a < arities.Length && IsUnityNull(created); a++)
                    {
                        int want = arities[a];
                        for (int i = 0; i < methods.Length && IsUnityNull(created); i++)
                        {
                            MethodInfo m = methods[i];
                            if (m.Name != "CreateFontAsset") continue;
                            ParameterInfo[] ps = m.GetParameters();
                            if (ps.Length != want || !ps[0].ParameterType.IsAssignableFrom(fontType)) continue;
                            try
                            {
                                object[] args;
                                if (want == 1)
                                {
                                    args = new object[] { srcFont };
                                }
                                else
                                {
                                    args = new object[want];
                                    args[0] = srcFont;
                                    args[1] = 44;   // samplingPointSize
                                    args[2] = 5;    // atlasPadding
                                    args[3] = CoerceValue(4165, ps[3].ParameterType); // SDFAA
                                    args[4] = 1024; // atlasWidth
                                    args[5] = 1024; // atlasHeight
                                    args[6] = CoerceValue(1, ps[6].ParameterType);    // AtlasPopulationMode.Dynamic
                                    if (want == 8) args[7] = true; // enableMultiAtlasSupport
                                }
                                created = m.Invoke(null, args);
                            }
                            catch
                            {
                            }
                        }
                    }
                }
            }

            if (IsUnityNull(created)) return null;

            ConfigureAndVerifyDynamicTmp(created, srcFont, "UL10nForge_Dyn_" + Path.GetFileNameWithoutExtension(filePath), liveShader);
            if (!SmokeTestDynamicTmp(created))
            {
                SafeWarn("Dynamic TMP font failed smoke test for: " + Path.GetFileName(filePath));
                return null;
            }
            return created;
        }

        private static object CreateDynamicTmpOsFallback(Type tmpFontType, Type fontType, object liveShader)
        {
            // 1. Try physical OS CJK font files (works across TMP 1.4 ~ Unity 6000!)
            string[] osPaths = FindWindowsCjkFontFiles(fontType);
            for (int i = 0; i < osPaths.Length; i++)
            {
                object f = CreateDynamicTmpFromFile(tmpFontType, fontType, osPaths[i], liveShader);
                if (!IsUnityNull(f)) return f;
            }

            // 2. Fallback: TMP 3.2+ / Unity 6000 OS family overload CreateFontAsset(string family, string style, int size)
            try
            {
                MethodInfo m = tmpFontType.GetMethod("CreateFontAsset", BindingFlags.Public | BindingFlags.Static, null, new Type[] { typeof(string), typeof(string), typeof(int) }, null);
                if (m != null)
                {
                    string[] families = new string[] { _config.OsFont, "Microsoft YaHei", "SimHei", "SimSun" };
                    for (int i = 0; i < families.Length; i++)
                    {
                        if (string.IsNullOrEmpty(families[i])) continue;
                        try
                        {
                            object f = m.Invoke(null, new object[] { families[i], "", 44 });
                            if (!IsUnityNull(f))
                            {
                                ConfigureAndVerifyDynamicTmp(f, null, "UL10nForge_OS_" + families[i], liveShader);
                                if (SmokeTestDynamicTmp(f)) return f;
                            }
                        }
                        catch
                        {
                        }
                    }
                }
            }
            catch
            {
            }
            return null;
        }

        private static string[] FindWindowsCjkFontFiles(Type fontType)
        {
            List<string> paths = new List<string>();
            string[] preferredNames = new string[] { "msyh.ttc", "msyh.ttf", "simhei.ttf", "simsun.ttc", "deng.ttf" };

            // Query Unity Font.GetPathsToOSFonts() first if available
            try
            {
                if (fontType != null)
                {
                    MethodInfo mi = fontType.GetMethod("GetPathsToOSFonts", BindingFlags.Public | BindingFlags.Static, null, Type.EmptyTypes, null);
                    if (mi != null)
                    {
                        List<object> osList = SnapshotList(mi.Invoke(null, null));
                        for (int p = 0; p < preferredNames.Length; p++)
                        {
                            for (int i = 0; i < osList.Count; i++)
                            {
                                string s = osList[i] as string;
                                if (!string.IsNullOrEmpty(s) && s.EndsWith(preferredNames[p], StringComparison.OrdinalIgnoreCase) && File.Exists(s))
                                {
                                    if (!paths.Contains(s)) paths.Add(s);
                                }
                            }
                        }
                    }
                }
            }
            catch
            {
            }

            // Direct Windows Fonts folder fallback (.NET 3.5 compatible)
            string winDir = Environment.GetEnvironmentVariable("WINDIR");
            if (string.IsNullOrEmpty(winDir)) winDir = @"C:\Windows";
            if (!string.IsNullOrEmpty(winDir))
            {
                string fontsDir = Path.Combine(winDir, "Fonts");
                for (int p = 0; p < preferredNames.Length; p++)
                {
                    string full = Path.Combine(fontsDir, preferredNames[p]);
                    if (File.Exists(full) && !paths.Contains(full)) paths.Add(full);
                }
            }
            return paths.ToArray();
        }

        private static void ConfigureAndVerifyDynamicTmp(object tmpFont, object srcFont, string assetName, object liveShader)
        {
            Type t = tmpFont.GetType();
            SetPropOrField(tmpFont, t, "name", assetName);
            SetPropOrField(tmpFont, t, "isMultiAtlasTexturesEnabled", true);
            SetPropOrField(tmpFont, t, "atlasPopulationMode", 1);
            HealTmpMaterialShader(tmpFont, liveShader);
            DontDestroy(tmpFont);
            if (!IsUnityNull(srcFont)) DontDestroy(srcFont);
            object mat = GetPropOrField(tmpFont, t, "material");
            if (!IsUnityNull(mat)) DontDestroy(mat);
            object atlasTexs = GetPropOrField(tmpFont, t, "atlasTextures");
            List<object> texList = SnapshotList(atlasTexs);
            for (int i = 0; i < texList.Count; i++) DontDestroy(texList[i]);
        }

        private static bool SmokeTestDynamicTmp(object tmpFont)
        {
            if (IsUnityNull(tmpFont)) return false;
            try
            {
                Type t = tmpFont.GetType();
                MethodInfo[] ms = t.GetMethods(BindingFlags.Public | BindingFlags.Instance);
                for (int i = 0; i < ms.Length; i++)
                {
                    MethodInfo m = ms[i];
                    if (m.Name != "TryAddCharacters") continue;
                    ParameterInfo[] ps = m.GetParameters();
                    if (ps.Length == 1 && ps[0].ParameterType == typeof(string))
                    {
                        m.Invoke(tmpFont, new object[] { "的一是在中「」×→" });
                        return GetTmpGlyphCount(tmpFont) >= 4;
                    }
                    if (ps.Length == 2 && ps[0].ParameterType == typeof(string) && ps[1].IsOut)
                    {
                        object[] args = new object[] { "的一是在中「」×→", null };
                        m.Invoke(tmpFont, args);
                        return GetTmpGlyphCount(tmpFont) >= 4;
                    }
                }
            }
            catch
            {
            }
            // If TryAddCharacters signature wasn't found, still accept if font created cleanly
            return !IsUnityNull(tmpFont);
        }

        // ── Stage C: Legacy TMP 1.0–1.3 Runtime SDF Builder (Unity 5.x/2017/2018/2019) ──

        private static object CreateLegacyTmpFromSdfPack(Type tmpFontType, string packPath, object liveShader)
        {
            if (tmpFontType == null || IsUnityNull(liveShader) || !File.Exists(packPath)) return null;
            Type texType = FindType("UnityEngine.Texture2D");
            Type texFmtType = FindType("UnityEngine.TextureFormat");
            Type matType = FindType("UnityEngine.Material");
            Type shaderType = FindType("UnityEngine.Shader");
            Type soType = FindType("UnityEngine.ScriptableObject");
            Type faceInfoType = FindType("TMPro.FaceInfo");
            Type glyphType = FindType("TMPro.TMP_Glyph");
            Type kerningType = FindType("TMPro.KerningTable");
            if (texType == null || texFmtType == null || matType == null || shaderType == null || soType == null || faceInfoType == null || glyphType == null)
            {
                return null;
            }

            using (FileStream fs = File.OpenRead(packPath))
            using (GZipStream gz = new GZipStream(fs, CompressionMode.Decompress))
            using (BinaryReader br = new BinaryReader(gz, Encoding.UTF8))
            {
                byte[] magic = br.ReadBytes(8);
                if (magic.Length != 8 || Encoding.ASCII.GetString(magic) != "UL10NSDF") return null;
                uint ver = br.ReadUInt32();
                if (ver != 1) return null;
                int atlasW = br.ReadInt32();
                int atlasH = br.ReadInt32();
                float gradScale = br.ReadSingle();
                ushort nameLen = br.ReadUInt16();
                string faceName = Encoding.UTF8.GetString(br.ReadBytes(nameLen));

                object face = Activator.CreateInstance(faceInfoType);
                SetPropOrField(face, faceInfoType, "Name", faceName);
                SetPropOrField(face, faceInfoType, "PointSize", br.ReadSingle());
                SetPropOrField(face, faceInfoType, "Scale", br.ReadSingle());
                SetPropOrField(face, faceInfoType, "LineHeight", br.ReadSingle());
                SetPropOrField(face, faceInfoType, "Baseline", br.ReadSingle());
                SetPropOrField(face, faceInfoType, "Ascender", br.ReadSingle());
                SetPropOrField(face, faceInfoType, "CapHeight", br.ReadSingle());
                SetPropOrField(face, faceInfoType, "Descender", br.ReadSingle());
                SetPropOrField(face, faceInfoType, "CenterLine", br.ReadSingle());
                SetPropOrField(face, faceInfoType, "SuperscriptOffset", br.ReadSingle());
                SetPropOrField(face, faceInfoType, "SubscriptOffset", br.ReadSingle());
                SetPropOrField(face, faceInfoType, "SubSize", br.ReadSingle());
                SetPropOrField(face, faceInfoType, "Underline", br.ReadSingle());
                SetPropOrField(face, faceInfoType, "UnderlineThickness", br.ReadSingle());
                SetPropOrField(face, faceInfoType, "strikethrough", br.ReadSingle());
                SetPropOrField(face, faceInfoType, "strikethroughThickness", br.ReadSingle());
                SetPropOrField(face, faceInfoType, "TabWidth", br.ReadSingle());
                SetPropOrField(face, faceInfoType, "Padding", br.ReadSingle());
                SetPropOrField(face, faceInfoType, "AtlasWidth", br.ReadSingle());
                SetPropOrField(face, faceInfoType, "AtlasHeight", br.ReadSingle());
                int charCount = br.ReadInt32();
                SetPropOrField(face, faceInfoType, "CharacterCount", charCount);

                int glyphCount = br.ReadInt32();
                Array glyphArr = Array.CreateInstance(glyphType, glyphCount);
                FieldInfo fId = glyphType.GetField("id", BindingFlags.Public | BindingFlags.Instance);
                FieldInfo fX = glyphType.GetField("x", BindingFlags.Public | BindingFlags.Instance);
                FieldInfo fY = glyphType.GetField("y", BindingFlags.Public | BindingFlags.Instance);
                FieldInfo fW = glyphType.GetField("width", BindingFlags.Public | BindingFlags.Instance);
                FieldInfo fH = glyphType.GetField("height", BindingFlags.Public | BindingFlags.Instance);
                FieldInfo fXo = glyphType.GetField("xOffset", BindingFlags.Public | BindingFlags.Instance);
                FieldInfo fYo = glyphType.GetField("yOffset", BindingFlags.Public | BindingFlags.Instance);
                FieldInfo fXa = glyphType.GetField("xAdvance", BindingFlags.Public | BindingFlags.Instance);
                FieldInfo fSc = glyphType.GetField("scale", BindingFlags.Public | BindingFlags.Instance);

                for (int i = 0; i < glyphCount; i++)
                {
                    object g = Activator.CreateInstance(glyphType);
                    int gid = br.ReadInt32();
                    float gx = br.ReadSingle();
                    float gy = br.ReadSingle();
                    float gw = br.ReadSingle();
                    float gh = br.ReadSingle();
                    float gxo = br.ReadSingle();
                    float gyo = br.ReadSingle();
                    float gxa = br.ReadSingle();
                    float gsc = br.ReadSingle();
                    if (fId != null) fId.SetValue(g, gid);
                    if (fX != null) fX.SetValue(g, gx);
                    if (fY != null) fY.SetValue(g, gy);
                    if (fW != null) fW.SetValue(g, gw);
                    if (fH != null) fH.SetValue(g, gh);
                    if (fXo != null) fXo.SetValue(g, gxo);
                    if (fYo != null) fYo.SetValue(g, gyo);
                    if (fXa != null) fXa.SetValue(g, gxa);
                    if (fSc != null) fSc.SetValue(g, gsc);
                    glyphArr.SetValue(g, i);
                }

                int pixelLen = br.ReadInt32();
                if (pixelLen != atlasW * atlasH) return null;
                byte[] pixels = br.ReadBytes(pixelLen);
                if (pixels.Length != pixelLen) return null;

                // Construct Texture2D(atlasW, atlasH, Alpha8, false)
                ConstructorInfo texCtor = texType.GetConstructor(new Type[] { typeof(int), typeof(int), texFmtType, typeof(bool) });
                if (texCtor == null) return null;
                object tex = texCtor.Invoke(new object[] { atlasW, atlasH, Enum.ToObject(texFmtType, 1), false });
                SetPropOrField(tex, texType, "name", "UL10nForge_Legacy_Atlas");
                MethodInfo loadRaw = null;
                MethodInfo[] texMs = texType.GetMethods(BindingFlags.Public | BindingFlags.Instance);
                for (int i = 0; i < texMs.Length; i++)
                {
                    if (texMs[i].Name == "LoadRawTextureData" && texMs[i].GetParameters().Length == 1)
                    {
                        ParameterInfo p0 = texMs[i].GetParameters()[0];
                        if (p0.ParameterType == typeof(byte[]) || p0.ParameterType.Name.IndexOf("StructArray", StringComparison.Ordinal) >= 0)
                        {
                            loadRaw = texMs[i];
                            break;
                        }
                    }
                }
                if (loadRaw == null) return null;
                loadRaw.Invoke(tex, new object[] { ConvertArrayArg(pixels, loadRaw.GetParameters()[0].ParameterType) });
                MethodInfo applyMi = texType.GetMethod("Apply", BindingFlags.Public | BindingFlags.Instance, null, new Type[] { typeof(bool), typeof(bool) }, null);
                if (applyMi != null) applyMi.Invoke(tex, new object[] { false, true });

                // Construct Material(liveShader)
                ConstructorInfo matCtor = matType.GetConstructor(new Type[] { shaderType });
                if (matCtor == null) return null;
                object mat = matCtor.Invoke(new object[] { liveShader });
                SetPropOrField(mat, matType, "name", "UL10nForge_Legacy_SDF_Mat");
                MethodInfo setTex = matType.GetMethod("SetTexture", BindingFlags.Public | BindingFlags.Instance, null, new Type[] { typeof(string), FindType("UnityEngine.Texture") }, null);
                MethodInfo setFloat = matType.GetMethod("SetFloat", BindingFlags.Public | BindingFlags.Instance, null, new Type[] { typeof(string), typeof(float) }, null);
                if (setTex != null) setTex.Invoke(mat, new object[] { "_MainTex", tex });
                if (setFloat != null)
                {
                    setFloat.Invoke(mat, new object[] { "_TextureWidth", (float)atlasW });
                    setFloat.Invoke(mat, new object[] { "_TextureHeight", (float)atlasH });
                    setFloat.Invoke(mat, new object[] { "_GradientScale", gradScale });
                    setFloat.Invoke(mat, new object[] { "_ScaleRatioA", 1f });
                    setFloat.Invoke(mat, new object[] { "_ScaleRatioB", 1f });
                    setFloat.Invoke(mat, new object[] { "_ScaleRatioC", 1f });
                    setFloat.Invoke(mat, new object[] { "_WeightBold", 0.75f });
                    setFloat.Invoke(mat, new object[] { "_WeightNormal", 0f });
                }

                // ScriptableObject.CreateInstance(TMP_FontAsset)
                object fontAsset = null;
                MethodInfo[] soMs = soType.GetMethods(BindingFlags.Public | BindingFlags.Static);
                for (int i = 0; i < soMs.Length; i++)
                {
                    if (soMs[i].Name == "CreateInstance" && !soMs[i].IsGenericMethod && soMs[i].GetParameters().Length == 1)
                    {
                        Type p0t = soMs[i].GetParameters()[0].ParameterType;
                        if (p0t == typeof(Type) || p0t.Name == "Type")
                        {
                            fontAsset = soMs[i].Invoke(null, new object[] { ToUnityTypeArg(tmpFontType, p0t) });
                            break;
                        }
                    }
                }
                if (IsUnityNull(fontAsset)) return null;

                SetPropOrField(fontAsset, tmpFontType, "name", "UL10nForge_Legacy_SDF");
                SetPropOrField(fontAsset, tmpFontType, "fontAssetType", 1); // SDF
                SetPropOrField(fontAsset, tmpFontType, "atlas", tex);
                SetPropOrField(fontAsset, tmpFontType, "material", mat);

                MethodInfo addFace = tmpFontType.GetMethod("AddFaceInfo", BindingFlags.Public | BindingFlags.Instance);
                if (addFace != null) addFace.Invoke(fontAsset, new object[] { face });

                MethodInfo addGlyphs = tmpFontType.GetMethod("AddGlyphInfo", BindingFlags.Public | BindingFlags.Instance);
                if (addGlyphs != null)
                {
                    Type gp = addGlyphs.GetParameters()[0].ParameterType;
                    addGlyphs.Invoke(fontAsset, new object[] { ConvertArrayArg(glyphArr, gp) });
                }

                if (kerningType != null)
                {
                    object kt = Activator.CreateInstance(kerningType);
                    MethodInfo addKern = tmpFontType.GetMethod("AddKerningInfo", BindingFlags.Public | BindingFlags.Instance);
                    if (addKern != null) addKern.Invoke(fontAsset, new object[] { kt });
                    else SetPropOrField(fontAsset, tmpFontType, "m_kerningInfo", kt);
                }

                MethodInfo readDef = tmpFontType.GetMethod("ReadFontDefinition", BindingFlags.Public | BindingFlags.Instance, null, Type.EmptyTypes, null);
                if (readDef != null) readDef.Invoke(fontAsset, null);

                DontDestroy(tex);
                DontDestroy(mat);
                DontDestroy(fontAsset);
                return fontAsset;
            }
        }

        private static void RefreshActiveTmpTexts()
        {
            try
            {
                Type tmpTextType = FindType("TMPro.TMP_Text");
                if (tmpTextType == null) return;
                MethodInfo fmu = tmpTextType.GetMethod("ForceMeshUpdate", BindingFlags.Public | BindingFlags.Instance, null, Type.EmptyTypes, null);
                if (fmu == null)
                {
                    fmu = tmpTextType.GetMethod("ForceMeshUpdate", BindingFlags.Public | BindingFlags.Instance, null, new Type[] { typeof(bool) }, null);
                }
                if (fmu == null) return;
                List<object> texts = FindSceneObjects(tmpTextType);
                int maxRefresh = texts.Count > 512 ? 512 : texts.Count;
                for (int i = 0; i < maxRefresh; i++)
                {
                    object t = texts[i];
                    if (IsUnityNull(t)) continue;
                    try
                    {
                        if (fmu.GetParameters().Length == 0) fmu.Invoke(t, null);
                        else fmu.Invoke(t, new object[] { false });
                    }
                    catch
                    {
                    }
                }
            }
            catch
            {
            }
        }

        // ── Stage E1: UI Toolkit (UIElements) TextSettings Fallback ───────────

        private static void GuardUiToolkit()
        {
            Type tsType = FindType("UnityEngine.TextCore.Text.TextSettings");
            Type faType = FindType("UnityEngine.TextCore.Text.FontAsset");
            if (tsType == null || faType == null) return;

            if (!_uiToolkitAttempted)
            {
                _uiToolkitAttempted = true;
                Type fontType = FindType("UnityEngine.Font");
                string ttfPath = _config.ResolvePath(_config.FontTtf);
                if (!string.IsNullOrEmpty(ttfPath) && File.Exists(ttfPath))
                {
                    _managedUiToolkitFont = CreateDynamicTextCoreFont(faType, fontType, ttfPath);
                }
                if (IsUnityNull(_managedUiToolkitFont))
                {
                    string[] osPaths = FindWindowsCjkFontFiles(fontType);
                    for (int i = 0; i < osPaths.Length && IsUnityNull(_managedUiToolkitFont); i++)
                    {
                        _managedUiToolkitFont = CreateDynamicTextCoreFont(faType, fontType, osPaths[i]);
                    }
                }
                if (!IsUnityNull(_managedUiToolkitFont))
                {
                    SafeInfo("UI Toolkit dynamic fallback font ready.");
                }
            }

            if (IsUnityNull(_managedUiToolkitFont)) return;
            List<object> settingsList = FindAllLoadedObjects(tsType);
            for (int i = 0; i < settingsList.Count; i++)
            {
                object ts = settingsList[i];
                if (IsUnityNull(ts)) continue;
                object fb = GetPropOrField(ts, ts.GetType(), "fallbackFontAssets");
                if (fb != null) ListAddItem(fb, _managedUiToolkitFont);
            }
        }

        private static object CreateDynamicTextCoreFont(Type faType, Type fontType, string filePath)
        {
            if (faType == null || fontType == null || string.IsNullOrEmpty(filePath) || !File.Exists(filePath)) return null;
            try
            {
                ConstructorInfo ctor = fontType.GetConstructor(new Type[] { typeof(string) });
                if (ctor == null) return null;
                object src = ctor.Invoke(new object[] { filePath });
                if (IsUnityNull(src)) return null;
                MethodInfo m = faType.GetMethod("CreateFontAsset", BindingFlags.Public | BindingFlags.Static, null, new Type[] { fontType }, null);
                if (m == null) return null;
                object fa = m.Invoke(null, new object[] { src });
                if (IsUnityNull(fa)) return null;
                SetPropOrField(fa, fa.GetType(), "isMultiAtlasTexturesEnabled", true);
                DontDestroy(src);
                DontDestroy(fa);
                return fa;
            }
            catch
            {
                return null;
            }
        }

        // ── Stage E2: 3D TextMesh, UGUI Text & NGUI UILabel Safety Net ──────

        private static void GuardLegacyTextComponents()
        {
            Type fontType = FindType("UnityEngine.Font");
            if (fontType == null) return;
            MethodInfo hasCharMi = fontType.GetMethod("HasCharacter", BindingFlags.Public | BindingFlags.Instance, null, new Type[] { typeof(char) }, null);
            if (hasCharMi == null) return;

            // 1. 3D UnityEngine.TextMesh (XUnity ChangeFont never swaps TextMesh.font!)
            Type tmType = FindType("UnityEngine.TextMesh");
            if (tmType != null)
            {
                Type mrType = FindType("UnityEngine.MeshRenderer");
                List<object> tms = FindSceneObjects(tmType);
                for (int i = 0; i < tms.Count; i++)
                {
                    object tm = tms[i];
                    if (IsUnityNull(tm)) continue;
                    string txt = GetPropOrField(tm, tmType, "text") as string;
                    char cjk = FindFirstCjkChar(txt);
                    if (cjk == '\0') continue;
                    object curFont = GetPropOrField(tm, tmType, "font");
                    if (!IsUnityNull(curFont))
                    {
                        bool has = false;
                        try { has = (bool)hasCharMi.Invoke(curFont, new object[] { cjk }); } catch { }
                        if (has) continue;
                    }
                    int fsize = 24;
                    object fsObj = GetPropOrField(tm, tmType, "fontSize");
                    if (fsObj != null) { try { fsize = Convert.ToInt32(fsObj); } catch { } }
                    object osFont = GetOrCreateOsFont(fontType, fsize);
                    if (IsUnityNull(osFont)) continue;
                    object oldMat = !IsUnityNull(curFont) ? GetPropOrField(curFont, fontType, "material") : null;
                    object newMat = GetPropOrField(osFont, fontType, "material");
                    SetPropOrField(tm, tmType, "font", osFont);
                    // Must also swap MeshRenderer.sharedMaterial so 3D TextMesh samples the new OS font texture!
                    if (mrType != null && !IsUnityNull(newMat))
                    {
                        try
                        {
                            MethodInfo getComp = null;
                            MethodInfo[] tmMs = tmType.GetMethods(BindingFlags.Public | BindingFlags.Instance);
                            for (int k = 0; k < tmMs.Length; k++)
                            {
                                if (tmMs[k].Name == "GetComponent" && !tmMs[k].IsGenericMethod && tmMs[k].GetParameters().Length == 1)
                                {
                                    Type p0t = tmMs[k].GetParameters()[0].ParameterType;
                                    if (p0t == typeof(Type) || p0t.Name == "Type")
                                    {
                                        getComp = tmMs[k];
                                        break;
                                    }
                                }
                            }
                            if (getComp != null)
                            {
                                object mr = getComp.Invoke(tm, new object[] { ToUnityTypeArg(mrType, getComp.GetParameters()[0].ParameterType) });
                                if (!IsUnityNull(mr))
                                {
                                    object curMat = GetPropOrField(mr, mrType, "sharedMaterial");
                                    if (IsUnityNull(curMat) || IsUnityNull(oldMat) || curMat.Equals(oldMat))
                                    {
                                        SetPropOrField(mr, mrType, "sharedMaterial", newMat);
                                    }
                                }
                            }
                        }
                        catch
                        {
                        }
                    }
                }
            }

            // 2. UGUI UnityEngine.UI.Text safety net (catches pre-translated / custom bitmap fonts skipped by XUnity)
            Type uiTextType = FindType("UnityEngine.UI.Text");
            if (uiTextType != null)
            {
                List<object> texts = FindSceneObjects(uiTextType);
                for (int i = 0; i < texts.Count; i++)
                {
                    object ut = texts[i];
                    if (IsUnityNull(ut)) continue;
                    string txt = GetPropOrField(ut, uiTextType, "text") as string;
                    char cjk = FindFirstCjkChar(txt);
                    if (cjk == '\0') continue;
                    object curFont = GetPropOrField(ut, uiTextType, "font");
                    if (!IsUnityNull(curFont))
                    {
                        bool has = false;
                        try { has = (bool)hasCharMi.Invoke(curFont, new object[] { cjk }); } catch { }
                        if (has) continue;
                    }
                    int fsize = 24;
                    object fsObj = GetPropOrField(ut, uiTextType, "fontSize");
                    if (fsObj != null) { try { fsize = Convert.ToInt32(fsObj); } catch { } }
                    object osFont = GetOrCreateOsFont(fontType, fsize);
                    if (!IsUnityNull(osFont))
                    {
                        SetPropOrField(ut, uiTextType, "font", osFont);
                    }
                }
            }

            // 3. NGUI UILabel safety net (only when using dynamic trueTypeFont)
            Type uiLabelType = FindType("UILabel");
            if (uiLabelType != null)
            {
                List<object> labels = FindSceneObjects(uiLabelType);
                for (int i = 0; i < labels.Count; i++)
                {
                    object lbl = labels[i];
                    if (IsUnityNull(lbl)) continue;
                    string txt = GetPropOrField(lbl, uiLabelType, "text") as string;
                    char cjk = FindFirstCjkChar(txt);
                    if (cjk == '\0') continue;
                    object curFont = GetPropOrField(lbl, uiLabelType, "trueTypeFont");
                    if (IsUnityNull(curFont)) continue;
                    bool has = false;
                    try { has = (bool)hasCharMi.Invoke(curFont, new object[] { cjk }); } catch { }
                    if (has) continue;
                    int fsize = 24;
                    object fsObj = GetPropOrField(lbl, uiLabelType, "fontSize");
                    if (fsObj != null) { try { fsize = Convert.ToInt32(fsObj); } catch { } }
                    object osFont = GetOrCreateOsFont(fontType, fsize);
                    if (!IsUnityNull(osFont))
                    {
                        SetPropOrField(lbl, uiLabelType, "trueTypeFont", osFont);
                    }
                }
            }
        }

        private static object GetOrCreateOsFont(Type fontType, int size)
        {
            int bucket = size <= 0 ? 24 : (size < 14 ? 14 : (size > 64 ? 64 : size));
            object cached;
            if (OsFontCache.TryGetValue(bucket, out cached) && !IsUnityNull(cached))
            {
                return cached;
            }
            if (OsFontCache.Count > 32) return null;
            try
            {
                MethodInfo mi = fontType.GetMethod("CreateDynamicFontFromOSFont", BindingFlags.Public | BindingFlags.Static, null, new Type[] { typeof(string), typeof(int) }, null);
                if (mi == null) return null;
                string[] cands = new string[] { _config.OsFont, "Microsoft YaHei", "SimHei", "SimSun" };
                for (int i = 0; i < cands.Length; i++)
                {
                    if (string.IsNullOrEmpty(cands[i])) continue;
                    object f = mi.Invoke(null, new object[] { cands[i], bucket });
                    if (!IsUnityNull(f))
                    {
                        DontDestroy(f);
                        OsFontCache[bucket] = f;
                        return f;
                    }
                }
            }
            catch
            {
            }
            return null;
        }
    }
}
