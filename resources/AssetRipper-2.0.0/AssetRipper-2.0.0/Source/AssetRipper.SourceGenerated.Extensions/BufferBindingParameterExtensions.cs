using AssetRipper.SourceGenerated.Subclasses.BufferBindingParameter;

namespace AssetRipper.SourceGenerated.Extensions;

public static class BufferBindingParameterExtensions
{
	public static void SetValues(this IBufferBindingParameter binding, string name, int index)
	{
		//binding.Name = name;//Name doesn't exist
		binding.NameIndex = -1;
		binding.Index = index;
		binding.ArraySize = 0;
	}
}
