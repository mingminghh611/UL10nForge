using AssetRipper.SourceGenerated.Enums;
using BuildTarget = AssetRipper.IO.Files.BuildTarget;

namespace AssetRipper.SourceGenerated.Extensions;

public static class BuildTargetGroupExtensions
{
	extension(BuildTargetGroup _this)
	{
		public static BuildTargetGroup FromBuildTarget(BuildTarget target)
		{
			switch (target)
			{
				case BuildTarget.StandaloneOSXUniversal:
				case BuildTarget.StandaloneOSXPPC:
				case BuildTarget.StandaloneOSXIntel:
				case BuildTarget.StandaloneWinPlayer:
				case BuildTarget.StandaloneLinux:
				case BuildTarget.StandaloneWin64Player:
				case BuildTarget.StandaloneLinux64:
				case BuildTarget.StandaloneLinuxUniversal:
				case BuildTarget.StandaloneOSXIntel64:
					return BuildTargetGroup.Standalone;

				case BuildTarget.WebPlayerLZMA:
				case BuildTarget.WebPlayerLZMAStreamed:
					return BuildTargetGroup.WebPlayer;

				case BuildTarget.Wii:
					return BuildTargetGroup.Wii;

				case BuildTarget.iOS:
					return BuildTargetGroup.iOS;

				case BuildTarget.PS3:
					return BuildTargetGroup.PS3;

				case BuildTarget.Xbox360:
					return BuildTargetGroup.XBOX360;

				case BuildTarget.Android:
					return BuildTargetGroup.Android;

				case BuildTarget.WinGLESEmu:
				case BuildTarget.WinGLES20Emu:
					return BuildTargetGroup.GLESEmu;

				case BuildTarget.GoogleNaCl:
					return BuildTargetGroup.NaCl;

				case BuildTarget.Flash:
					return BuildTargetGroup.FlashPlayer;

				case BuildTarget.WebGL:
					return BuildTargetGroup.WebGL;

				case BuildTarget.MetroPlayerX86:
				case BuildTarget.MetroPlayerX64:
				case BuildTarget.MetroPlayerARM:
					return BuildTargetGroup.WSA;

				case BuildTarget.WP8Player:
					return BuildTargetGroup.WP8;

				case BuildTarget.BB10:
					return BuildTargetGroup.BlackBerry;

				case BuildTarget.Tizen:
					return BuildTargetGroup.Tizen;

				case BuildTarget.PSP2:
					return BuildTargetGroup.PSP2;

				case BuildTarget.PS4:
					return BuildTargetGroup.PS4;

				case BuildTarget.PSM:
					return BuildTargetGroup.PSM;

				case BuildTarget.XboxOne:
					return BuildTargetGroup.XboxOne;

				case BuildTarget.SamsungTV:
					return BuildTargetGroup.SamsungTV;

				case BuildTarget.N3DS:
					return BuildTargetGroup.N3DS;

				case BuildTarget.WiiU:
					return BuildTargetGroup.WiiU;

				case BuildTarget.tvOS:
					return BuildTargetGroup.tvOS;

				case BuildTarget.Switch:
					return BuildTargetGroup.Switch;

				default:
					throw new NotSupportedException($"Platform {target} is not supported.");
			}
		}

		public string ToExportString()
		{
			return _this switch
			{
				BuildTargetGroup.Unknown => "Unknown",
				BuildTargetGroup.Standalone => "Standalone",
				BuildTargetGroup.WebPlayer => "WebPlayer",
				BuildTargetGroup.Wii => "Wii",
				BuildTargetGroup.iOS => "iPhone",
				BuildTargetGroup.PS3 => "PS3",
				BuildTargetGroup.XBOX360 => "XBOX360",
				BuildTargetGroup.Android => "Android",
				BuildTargetGroup.GLESEmu => "GLESEmu",
				BuildTargetGroup.NaCl => "NaCl",
				BuildTargetGroup.FlashPlayer => "FlashPlayer",
				BuildTargetGroup.WebGL => "WebGL",
				BuildTargetGroup.Metro => "Windows Store Apps",
				BuildTargetGroup.WP8 => "WP8",
				BuildTargetGroup.BlackBerry => "BlackBerry",
				BuildTargetGroup.Tizen => "Tizen",
				BuildTargetGroup.PSP2 => "PSP2",
				BuildTargetGroup.PS4 => "PS4",
				BuildTargetGroup.PSM => "PSM",
				BuildTargetGroup.XboxOne => "XboxOne",
				BuildTargetGroup.SamsungTV => "SamsungTV",
				BuildTargetGroup.N3DS => "Nintendo 3DS",
				BuildTargetGroup.WiiU => "WiiU",
				BuildTargetGroup.tvOS => "tvOS",
				BuildTargetGroup.Facebook => "Facebook",
				BuildTargetGroup.Switch => "Nintendo Switch",
				_ => throw new NotSupportedException($"Value: {_this}"),
			};
		}
	}
}
