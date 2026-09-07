using System.Text.Json.Serialization.Metadata;

namespace AssetRipper.Configuration;

public class JsonDataInstance<T> : DataInstance<T> where T : new()
{
	public JsonDataInstance(JsonTypeInfo<T> typeInfo) : base(new JsonDataSerializer<T>(typeInfo))
	{
	}
}
