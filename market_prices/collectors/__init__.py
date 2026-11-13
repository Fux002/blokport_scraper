"""Collectors per site platform. A site names its platform in the register; a new shop on one is config only."""

from market_prices.collectors.woocommerce_store import WooCommerceStoreCollector

COLLECTORS = {WooCommerceStoreCollector.platform: WooCommerceStoreCollector()}
