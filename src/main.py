# -*- coding: utf-8 -*-
"""
Notícias que você talvez vai se importar
Main entry point for the news crawler
"""

import os
import sys

# Add src to path for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from crawler import crawl_all_feeds, save_json


def main():
    """Main entry point."""
    # Configure stdout to use UTF-8 to prevent UnicodeEncodeError on Windows
    if hasattr(sys.stdout, 'reconfigure'):
        try:
            sys.stdout.reconfigure(encoding='utf-8')
        except Exception:
            pass
            
    # Determine output path
    script_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(script_dir)
    output_path = os.path.join(project_root, "docs", "data", "latest.json")
    
    print("🗞️  Notícias que você talvez vai se importar")
    print("=" * 50)
    
    # Run crawler
    data = crawl_all_feeds()
    
    # Save results
    save_json(data, output_path)
    
    print(f"\n📊 Summary:")
    print(f"   - Total articles: {data['metadata']['total_articles']}")
    print(f"   - Capa articles: {len(data['capa'])}")
    print(f"   - Categories: {len(data['categories'])}")
    print(f"   - Output: {output_path}")
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
