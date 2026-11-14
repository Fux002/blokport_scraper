"""Collectors per site platform. A site names its platform in the register; a new shop on one is config only."""

from market_prices.collectors.schema_org_product import SchemaOrgProductCollector
from market_prices.collectors.supplier_scrape import SupplierScrapeCollector
from market_prices.collectors.woocommerce_store import WooCommerceStoreCollector

COLLECTORS = {collector.platform: collector
              for collector in (WooCommerceStoreCollector(), SchemaOrgProductCollector(), SupplierScrapeCollector())}
