import Foundation
import Security
let input = FileHandle.standardInput.readDataToEndOfFile()
guard let request = try? JSONSerialization.jsonObject(with: input) as? [String: String], let account = request["account"], let action = request["action"] else { exit(2) }
let query: [String: Any] = [kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: "local-llm-desk.naver-pop3", kSecAttrAccount as String: account]
var status: OSStatus
if action == "save", let password = request["password"] {
    let data = Data(password.utf8)
    status = SecItemUpdate(query as CFDictionary, [kSecValueData as String: data] as CFDictionary)
    if status == errSecItemNotFound {
        var add = query; add[kSecValueData as String] = data
        status = SecItemAdd(add as CFDictionary, nil)
    }
} else {
    var read = query
    read[kSecReturnData as String] = action == "read"
    read[kSecMatchLimit as String] = kSecMatchLimitOne
    var result: CFTypeRef?
    status = SecItemCopyMatching(read as CFDictionary, &result)
    if status == errSecSuccess && action == "read", let data = result as? Data {
        FileHandle.standardOutput.write(data)
    }
}
if status != errSecSuccess { exit(status == errSecItemNotFound ? 3 : 1) }
