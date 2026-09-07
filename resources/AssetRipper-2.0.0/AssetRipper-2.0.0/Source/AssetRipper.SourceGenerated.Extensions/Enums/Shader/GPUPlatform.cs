namespace AssetRipper.SourceGenerated.Extensions.Enums.Shader;

/// <summary>
/// Graphic API. Also called ShaderCompilerPlatform<br/>
/// <see href="https://github.com/Unity-Technologies/UnityCsReference/blob/master/Editor/Mono/Graphics/ShaderCompilerData.cs"/>
/// </summary>
public enum GPUPlatform
{
	/// <summary>
	/// For inner use only
	/// </summary>
	Unknown = -1,
	/// <summary>
	/// For non initialized variable.
	/// </summary>
	None = 0,
	OpenGL = 0,
	/// <summary>
	/// Direct3D 9
	/// </summary>
	D3D9 = 1,
	/// <summary>
	/// Microsoft Xbox 360
	/// </summary>
	Xbox360 = 2,
	PS3 = 3,
	/// <summary>
	/// Direct3D 11 (FL10.0 and up) and Direct3D 12, compiled with MS D3DCompiler
	/// </summary>
	D3D11 = 4,
	/// <summary>
	/// OpenGL ES 2.0 / WebGL 1.0, compiled with MS D3DCompiler + HLSLcc
	/// </summary>
	Gles20 = 5,
	/// <summary>
	/// OpenGL ES 3.0+ / WebGL 2.0, compiled with MS D3DCompiler + HLSLcc
	/// </summary>
	/// <remarks>
	/// Google Native Client
	/// </remarks>
	GlesDesktop = 6,
	Flash = 7,
	D3D11_9x = 8,
	/// <summary>
	/// OpenGL ES 3.x and WebGL 2.0 graphics APIs on Android, iOS, Windows and WebGL platforms
	/// </summary>
	Gles3x = 9,
	/// <summary>
	/// PlayStation Vita
	/// </summary>
	/// <remarks>
	/// The native enum refers to this as PSP2
	/// </remarks>
	Vita = 10,
	/// <summary>
	/// Sony PS4
	/// </summary>
	PS4 = 11,
	/// <summary>
	/// Microsoft XboxOne
	/// </summary>
	XboxOne = 12,
	/// <summary>
	/// PlayStation Mobile
	/// </summary>
	PSM = 13,
	/// <summary>
	/// Apple Metal, compiled with MS D3DCompiler + HLSLcc
	/// </summary>
	Metal = 14,
	/// <summary>
	/// Desktop OpenGL 3+, compiled with MS D3DCompiler + HLSLcc
	/// </summary>
	GlCore = 15,
	/// <summary>
	/// Nintendo 3DS
	/// </summary>
	N3DS = 16,
	/// <summary>
	/// Nintendo Wii U
	/// </summary>
	WiiU = 17,
	/// <summary>
	/// Vulkan SPIR-V, compiled with MS D3DCompiler + HLSLcc
	/// </summary>
	Vulkan = 18,
	/// <summary>
	/// Nintendo Switch (NVN)
	/// </summary>
	Switch = 19,
	/// <summary>
	/// Xbox One D3D12
	/// </summary>
	XboxOne_D3D12 = 20,
	/// <summary>
	/// Game Core
	/// </summary>
	GameCoreXboxOne = 21,
	GameCoreSeries = 22,
	/// <summary>
	/// PlayStation 5
	/// </summary>
	PS5 = 23,
	/// <summary>
	/// PlayStation 5 NGGC
	/// </summary>
	PS5NGGC = 24,
	/// <summary>
	/// Direct3D 12 graphics API on Game Core platforms.
	/// </summary>
	GameCore_25 = 25,
	/// <summary>
	/// WebGPU graphics API
	/// </summary>
	WebGPU = 26,
	/// <summary>
	/// Nintendo Switch 2
	/// </summary>
	Switch2 = 27
}
public static class GPUPlatformExtensions
{
	public static bool IsDirectX(this GPUPlatform platform)
	{
		return platform is GPUPlatform.D3D9 or GPUPlatform.D3D11 or GPUPlatform.D3D11_9x;
	}

	public static bool IsOpenGL(this GPUPlatform platform)
	{
		return platform is GPUPlatform.OpenGL or GPUPlatform.Gles20 or GPUPlatform.GlesDesktop or GPUPlatform.Gles3x or GPUPlatform.GlCore;
	}
}
