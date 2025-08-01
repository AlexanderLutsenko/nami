# Unzip all zip files in ${data_dir} and remove them

for zipfile in "${data_dir}"/*.zip; do
    if [ -f "$zipfile" ]; then
        unzip "$zipfile" -d "${data_dir}" && rm "$zipfile"
    fi
done
