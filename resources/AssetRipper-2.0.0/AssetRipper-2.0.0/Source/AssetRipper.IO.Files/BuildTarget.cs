namespace AssetRipper.IO.Files;

/// <summary>
/// <see href="https://github.com/Unity-Technologies/UnityCsReference/blob/master/Editor/Mono/BuildTarget.cs"/>
/// </summary>
public enum BuildTarget : uint
{
	ValidPlayer = 1,
	/// <summary>
	/// Universal macOS standalone
	/// </summary>
	StandaloneOSXUniversal = 2,
	/// <summary>
	/// macOS standalone (PowerPC only)
	/// </summary>
	StandaloneOSXPPC = 3,
	/// <summary>
	/// macOS standalone (Intel only)
	/// </summary>
	StandaloneOSXIntel = 4,
	/// <summary>
	/// Windows standalone
	/// </summary>
	StandaloneWinPlayer = 5,
	/// <summary>
	/// Web player
	/// </summary>
	WebPlayerLZMA = 6,
	/// <summary>
	/// Streamed web player
	/// </summary>
	WebPlayerLZMAStreamed = 7,
	/// <summary>
	/// Nintendo Wii
	/// </summary>
	Wii = 8,
	/// <summary>
	/// iOS player
	/// </summary>
	iOS = 9,
	/// <summary>
	/// PlayStation 3
	/// </summary>
	PS3 = 10,
	Xbox360 = 11,
	Broadcom = 12,
	/// <summary>
	/// Android .apk standalone app
	/// </summary>
	Android = 13,
	WinGLESEmu = 14,
	WinGLES20Emu = 15,
	/// <summary>
	/// Google Native Client
	/// </summary>
	GoogleNaCl = 16,
	/// <summary>
	/// Linux standalone
	/// </summary>
	StandaloneLinux = 17,
	Flash = 18,
	/// <summary>
	/// Windows 64-bit standalone
	/// </summary>
	StandaloneWin64Player = 19,
	/// <summary>
	/// WebGL
	/// </summary>
	WebGL = 20,
	/// <summary>
	/// Windows Store Apps player
	/// </summary>
	MetroPlayerX86 = 21,
	/// <summary>
	/// Windows Store Apps player
	/// </summary>
	MetroPlayerX64 = 22,
	/// <summary>
	/// Windows Store Apps player
	/// </summary>
	MetroPlayerARM = 23,
	/// <summary>
	/// Linux 64-bit standalone
	/// </summary>
	StandaloneLinux64 = 24,
	/// <summary>
	/// Linux universal standalone
	/// </summary>
	StandaloneLinuxUniversal = 25,
	/// <summary>
	/// Windows Phone 8 player
	/// </summary>
	WP8Player = 26,
	/// <summary>
	/// macOS Intel 64-bit standalone
	/// </summary>
	StandaloneOSXIntel64 = 27,
	/// <summary>
	/// BlackBerry
	/// </summary>
	BB10 = 28,
	/// <summary>
	/// Tizen player
	/// </summary>
	Tizen = 29,
	/// <summary>
	/// PS Vita Standalone
	/// </summary>
	PSP2 = 30,
	/// <summary>
	/// PS4 Standalone
	/// </summary>
	PS4 = 31,
	/// <summary>
	/// PlayStation Mobile
	/// </summary>
	PSM = 32,
	/// <summary>
	/// Xbox One Standalone
	/// </summary>
	XboxOne = 33,
	/// <summary>
	/// Samsung Smart TV
	/// </summary>
	SamsungTV = 34,
	/// <summary>
	/// Nintendo 3DS
	/// </summary>
	N3DS = 35,
	/// <summary>
	/// Wii U standalone
	/// </summary>
	WiiU = 36,
	/// <summary>
	/// Apple tvOS
	/// </summary>
	tvOS = 37,
	/// <summary>
	/// Nintendo Switch player
	/// </summary>
	Switch = 38,
	Lumin = 39,
	/// <summary>
	/// Stadia standalone
	/// </summary>
	Stadia = 40,
	CloudRendering = 41,
	/// <summary>
	/// Xbox Series player
	/// </summary>
	GameCoreXboxSeries = 42,
	/// <summary>
	/// Xbox one player
	/// </summary>
	GameCoreXboxOne = 43,
	/// <summary>
	/// PS5 Standalone
	/// </summary>
	PS5 = 44,
	EmbeddedLinux = 45,
	QNX = 46,
	/// <summary>
	/// Apple Vision OS
	/// </summary>
	VisionOS = 47,
	/// <summary>
	/// Nintendo Switch 2
	/// </summary>
	Switch2 = 48,
	Kepler = 49,

	/// <summary>
	/// Editor
	/// </summary>
	NoTarget = 0xFFFFFFFE,
	AnyPlayer = 0xFFFFFFFF,
}
public static class BuildTargetExtensions
{
	extension(BuildTarget target)
	{
		public bool IsStandalone
		{
			get
			{
				switch (target)
				{
					case BuildTarget.StandaloneWinPlayer:
					case BuildTarget.StandaloneWin64Player:
					case BuildTarget.StandaloneLinux:
					case BuildTarget.StandaloneLinux64:
					case BuildTarget.StandaloneLinuxUniversal:
					case BuildTarget.StandaloneOSXIntel:
					case BuildTarget.StandaloneOSXIntel64:
					case BuildTarget.StandaloneOSXPPC:
					case BuildTarget.StandaloneOSXUniversal:
						return true;
					default:
						return false;
				}
			}
		}

		public bool IsCompatible(BuildTarget comp)
		{
			return target == comp || (target.IsStandalone && comp.IsStandalone);
		}
	}
}
