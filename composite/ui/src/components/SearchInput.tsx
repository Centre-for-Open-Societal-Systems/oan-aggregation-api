"use client";

import { Search, X } from "lucide-react";
import { forwardRef } from "react";

interface SearchInputProps {
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  width?: string;
  className?: string;
}

const SearchInput = forwardRef<HTMLInputElement, SearchInputProps>(
  ({ value, onChange, placeholder, width = "w-60", className = "" }, ref) => {
    return (
      <div className={`relative ${width}`}>
        <input
          ref={ref}
          type="text"
          value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder={placeholder}
          className={`h-9 w-full rounded-[10px] border border-[#ED7C22] bg-white pl-3 pr-9 text-[16px] text-black placeholder:text-gray-600 focus:outline-none ${className}`}
        />
        {value ? (
          <button
            type="button"
            onClick={() => onChange("")}
            className="absolute right-3 top-1/2 -translate-y-1/2 text-gray-400 hover:text-gray-600 cursor-pointer"
            aria-label="Clear"
          >
            <X size={14} />
          </button>
        ) : (
          <Search
            size={14}
            className="absolute right-3 top-1/2 -translate-y-1/2 text-black"
          />
        )}
      </div>
    );
  }
);

SearchInput.displayName = "SearchInput";

export default SearchInput;
