from langchain_community.tools import DuckDuckGoSearchResults

web_search = DuckDuckGoSearchResults(
    name="web_search",
    description="在互联网上搜索最新信息。"
)